from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from conftest import BACKEND, DRIVE_READONLY, make_token
from gdrive_reader import tools
from gdrive_reader.errors import NotAuthenticated, ReauthRequired
from gdrive_reader.oauth import LoginFlow
from gdrive_reader.services import Services
from gdrive_reader.token_store import TokenStore


def _hit_loopback(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - local test server
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _start_flow(services: Services):
    outcome = services.auth.connect(wait_seconds=0)
    assert outcome.result.status == "pending"
    query = parse_qs(urlparse(outcome.authorize_url).query)
    state = query["state"][0]
    port = int(state.split(".")[0])
    assert query["code_challenge"][0]
    assert outcome.authorize_url.startswith(f"{BACKEND}/oauth/start?")
    return state, port


def test_login_flow_exchanges_code_and_stores_token(services: Services, mock_http: respx.MockRouter) -> None:
    mock_http.route(host="127.0.0.1").pass_through()
    token_route = mock_http.post(f"{BACKEND}/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "ya29.a", "refresh_token": "1//r", "expires_in": 3599, "scope": DRIVE_READONLY},
        )
    )
    state, port = _start_flow(services)

    status, page = _hit_loopback(f"http://127.0.0.1:{port}/callback?code=abc&state={state}")

    assert status == 200 and "授權完成" in page
    sent = token_route.calls.last.request
    assert b'"code":"abc"' in sent.content and b"code_verifier" in sent.content
    assert services.auth._flow.result.status == "success"  # noqa: SLF001
    stored = services.auth.stored_token()
    assert stored is not None and stored.refresh_token == "1//r"


def test_login_flow_rejects_wrong_state_and_keeps_waiting(services: Services, mock_http: respx.MockRouter) -> None:
    mock_http.route(host="127.0.0.1").pass_through()
    state, port = _start_flow(services)

    status, _ = _hit_loopback(f"http://127.0.0.1:{port}/callback?code=abc&state={port}.forged")

    assert status == 400
    assert services.auth.pending_login() is not None
    assert services.auth.stored_token() is None
    services.auth.disconnect()


def test_login_flow_reports_missing_drive_scope(services: Services, mock_http: respx.MockRouter) -> None:
    mock_http.route(host="127.0.0.1").pass_through()
    mock_http.post(f"{BACKEND}/oauth/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3599, "scope": "openid"}
        )
    )
    state, port = _start_flow(services)
    status, _ = _hit_loopback(f"http://127.0.0.1:{port}/callback?code=abc&state={state}")
    assert status == 400
    flow_result = services.auth._flow.result  # noqa: SLF001
    assert flow_result.status == "error" and "勾選" in flow_result.message
    assert services.auth.stored_token() is None


def test_user_cancel_is_reported(services: Services, mock_http: respx.MockRouter) -> None:
    mock_http.route(host="127.0.0.1").pass_through()
    state, port = _start_flow(services)
    _hit_loopback(f"http://127.0.0.1:{port}/callback?error=access_denied&state={state}")
    assert "取消" in services.auth._flow.result.message  # noqa: SLF001


def test_access_token_refreshes_when_expiring(services: Services, mock_http: respx.MockRouter) -> None:
    services.auth._store.save(make_token(expires_in=10))  # noqa: SLF001
    route = mock_http.post(f"{BACKEND}/oauth/refresh").mock(
        return_value=httpx.Response(200, json={"access_token": "fresh", "expires_in": 3599, "scope": DRIVE_READONLY})
    )
    assert services.auth.access_token() == "fresh"
    assert route.called
    assert services.auth.stored_token().refresh_token == "refresh-1"


def test_invalid_grant_clears_token_and_asks_for_reauth(services: Services, mock_http: respx.MockRouter) -> None:
    services.auth._store.save(make_token(expires_in=10))  # noqa: SLF001
    mock_http.post(f"{BACKEND}/oauth/refresh").mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(ReauthRequired):
        services.auth.access_token()
    assert services.auth.stored_token() is None
    with pytest.raises(NotAuthenticated):
        services.auth.access_token()


def test_disconnect_revokes_and_deletes(services: Services, mock_http: respx.MockRouter) -> None:
    services.auth._store.save(make_token())  # noqa: SLF001
    revoke = mock_http.post("https://oauth2.googleapis.com/revoke").mock(return_value=httpx.Response(200))
    assert "撤銷" in tools.disconnect(services)
    assert revoke.calls.last.request.content == b"token=refresh-1"
    assert services.auth.stored_token() is None


def test_connect_short_circuits_when_already_connected(services: Services, mock_http: respx.MockRouter) -> None:
    services.auth._store.save(make_token())  # noqa: SLF001
    mock_http.get("https://www.googleapis.com/drive/v3/about").mock(
        return_value=httpx.Response(200, json={"user": {"emailAddress": "me@example.com", "displayName": "Me"}})
    )
    assert "me@example.com" in tools.connect(services, wait_seconds=0)
    assert services.opened_urls == []  # type: ignore[attr-defined]


class _SlowBroker:
    """Code exchange that takes a while, so the flow can be cancelled mid-flight."""

    def authorize_url(self, code_challenge: str, state: str) -> str:
        return f"{BACKEND}/oauth/start?state={state}"

    def exchange_code(self, code: str, code_verifier: str) -> dict:
        time.sleep(0.5)
        return {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": DRIVE_READONLY}


def test_cancel_during_code_exchange_does_not_persist_token(tmp_path, mock_http: respx.MockRouter) -> None:
    mock_http.route(host="127.0.0.1").pass_through()
    store = TokenStore(tmp_path / "token.json")
    flow = LoginFlow(_SlowBroker(), store)  # type: ignore[arg-type]
    port = flow.state.split(".")[0]
    result: dict[str, tuple[int, str]] = {}
    browser = threading.Thread(
        target=lambda: result.update(page=_hit_loopback(f"http://127.0.0.1:{port}/callback?code=c&state={flow.state}"))
    )
    browser.start()
    time.sleep(0.15)
    flow.cancel()  # e.g. disconnect_google, or the 10-minute timer firing
    browser.join()

    assert flow.result.status == "error"
    assert store.load() is None
    assert result["page"][0] == 400 and "沒有儲存" in result["page"][1]
