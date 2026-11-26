from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from worker import GOOGLE_TOKEN_URL, create_app

CHALLENGE = "A" * 43
STATE = "53682.abcdefghijklmnop0123"
VERIFIER = "v" * 64


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "shh")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    return TestClient(create_app(), base_url="https://broker.test", follow_redirects=False)


def test_start_redirects_to_google_with_pkce_and_offline_access(client: TestClient) -> None:
    resp = client.get("/oauth/start", params={"code_challenge": CHALLENGE, "state": STATE})
    assert resp.status_code == 302
    url = urlparse(resp.headers["location"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "accounts.google.com"
    assert query["client_id"] == "cid.apps.googleusercontent.com"
    assert query["redirect_uri"] == "https://broker.test/oauth/callback"
    assert query["scope"] == "https://www.googleapis.com/auth/drive.readonly"
    assert query["code_challenge_method"] == "S256" and query["code_challenge"] == CHALLENGE
    assert query["access_type"] == "offline" and query["state"] == STATE
    assert "shh" not in resp.headers["location"]
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "params",
    [
        {"code_challenge": "short", "state": STATE},
        {"code_challenge": CHALLENGE, "state": "nope"},
        {"code_challenge": CHALLENGE, "state": "80.abcdefghijklmnop"},  # privileged port
        {},
    ],
)
def test_start_rejects_bad_parameters(client: TestClient, params: dict) -> None:
    assert client.get("/oauth/start", params=params).status_code == 400


def test_callback_relays_only_to_loopback(client: TestClient) -> None:
    resp = client.get("/oauth/callback", params={"code": "4/abc", "state": STATE, "scope": "x", "extra": "y"})
    assert resp.status_code == 302
    location = urlparse(resp.headers["location"])
    assert location.scheme == "http" and location.netloc == "127.0.0.1:53682" and location.path == "/callback"
    assert parse_qs(location.query) == {"code": ["4/abc"], "state": [STATE]}


def test_callback_relays_google_errors(client: TestClient) -> None:
    resp = client.get("/oauth/callback", params={"error": "access_denied", "state": STATE})
    assert parse_qs(urlparse(resp.headers["location"]).query)["error"] == ["access_denied"]


def test_callback_rejects_bad_state(client: TestClient) -> None:
    assert client.get("/oauth/callback", params={"code": "x", "state": "evil.com"}).status_code == 400


@respx.mock
def test_token_exchange_adds_secret_and_filters_response(client: TestClient) -> None:
    google = respx.post(GOOGLE_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "ya29.x",
                "refresh_token": "1//r",
                "expires_in": 3599,
                "scope": "s",
                "token_type": "Bearer",
                "id_token": "drop-me",
            },
        )
    )
    resp = client.post("/oauth/token", json={"code": "4/abc", "code_verifier": VERIFIER})
    assert resp.status_code == 200
    assert resp.json() == {
        "access_token": "ya29.x",
        "refresh_token": "1//r",
        "expires_in": 3599,
        "scope": "s",
        "token_type": "Bearer",
    }
    form = parse_qs(google.calls.last.request.content.decode())
    assert form["client_secret"] == ["shh"] and form["code_verifier"] == [VERIFIER]
    assert form["redirect_uri"] == ["https://broker.test/oauth/callback"]
    assert resp.headers["cache-control"] == "no-store"


@respx.mock
def test_refresh_passes_invalid_grant_through(client: TestClient) -> None:
    respx.post(GOOGLE_TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    resp = client.post("/oauth/refresh", json={"refresh_token": "1//r"})
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant"


@respx.mock
def test_google_outage_is_502(client: TestClient) -> None:
    respx.post(GOOGLE_TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
    assert client.post("/oauth/refresh", json={"refresh_token": "1//r"}).status_code == 502


def test_token_rejects_malformed_verifier(client: TestClient) -> None:
    assert client.post("/oauth/token", json={"code": "c", "code_verifier": "short"}).status_code == 422


def test_missing_configuration_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    client = TestClient(create_app())
    assert client.get("/healthz").json() == {"ok": True, "configured": False}
    resp = client.post("/oauth/refresh", json={"refresh_token": "r"})
    assert resp.status_code == 500 and resp.json()["error"] == "server_misconfigured"


def test_cloudflare_env_binding_is_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Workers, secrets arrive as request.scope["env"], not os.environ."""
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    inner = create_app()
    env = SimpleNamespace(GOOGLE_CLIENT_ID="from-env", GOOGLE_CLIENT_SECRET="s", PUBLIC_BASE_URL="https://pub.example")

    async def with_env(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "env": env}
        await inner(scope, receive, send)

    client = TestClient(with_env, follow_redirects=False)
    resp = client.get("/oauth/start", params={"code_challenge": CHALLENGE, "state": STATE})
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["client_id"] == ["from-env"]
    assert query["redirect_uri"] == ["https://pub.example/oauth/callback"]


def test_home_page_describes_the_app_and_links_the_privacy_policy(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    assert "Google Drive" in resp.text and 'href="/privacy"' in resp.text


def test_privacy_policy_covers_google_user_data(client: TestClient) -> None:
    resp = client.get("/privacy")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    assert "drive.readonly" in resp.text and "Limited Use" in resp.text
    assert "myaccount.google.com/permissions" in resp.text
