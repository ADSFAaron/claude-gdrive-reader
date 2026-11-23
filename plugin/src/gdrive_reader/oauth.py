"""Browser login: Authorization Code + PKCE, brokered by the deployed backend.

Flow (see docs/ARCHITECTURE.md for the diagram):

1. Generate a PKCE verifier/challenge and a random nonce; start a one-shot HTTP
   listener on 127.0.0.1 with an OS-assigned port. ``state`` = ``<port>.<nonce>``.
2. Open ``<backend>/oauth/start?code_challenge=...&state=...``; the backend adds
   the client ID and redirects to Google's consent screen.
3. Google redirects to ``<backend>/oauth/callback``; the backend relays ``code``
   and ``state`` to ``http://127.0.0.1:<port>/callback``.
4. The listener checks ``state``, then sends ``code`` + verifier to
   ``<backend>/oauth/token``; only the backend knows the client secret.

The authorization code passes through the browser, but it is useless without
the verifier, which never leaves this process.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .errors import BrokerError
from .token_store import StoredToken, TokenStore

log = logging.getLogger(__name__)

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
CALLBACK_PATH = "/callback"


def make_pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, S256 challenge)`` as defined in RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class BrokerClient:
    """Talks to the OAuth backend (backend/src/worker.py)."""

    def __init__(self, base_url: str, http: httpx.Client) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http

    def authorize_url(self, code_challenge: str, state: str) -> str:
        query = urlencode({"code_challenge": code_challenge, "state": state})
        return f"{self.base_url}/oauth/start?{query}"

    def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        return self._post("/oauth/token", {"code": code, "code_verifier": code_verifier})

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return self._post("/oauth/refresh", {"refresh_token": refresh_token})

    def _post(self, path: str, payload: dict[str, str]) -> dict[str, Any]:
        try:
            resp = self._http.post(f"{self.base_url}{path}", json=payload)
        except httpx.HTTPError as exc:
            raise BrokerError(
                f"無法連線到 OAuth 後端（{self.base_url}）。請確認網路連線，或後端服務是否正常運作。",
                code="network",
            ) from exc

        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 200 and isinstance(body, dict) and body.get("access_token"):
            return body

        error = body.get("error") if isinstance(body, dict) else None
        if error == "invalid_grant":
            raise BrokerError("Google 拒絕了這組授權（invalid_grant）。", code="invalid_grant")
        if error == "server_misconfigured":
            raise BrokerError("OAuth 後端尚未設定 Google OAuth Client，請檢查後端的環境變數。", code=error)
        raise BrokerError(
            f"OAuth 後端回傳錯誤（HTTP {resp.status_code}，{error or 'unknown'}）。請稍後再試。",
            code=error or f"http_{resp.status_code}",
        )


_PAGE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<title>Google Drive Reader</title></head>
<body style="font-family:system-ui,sans-serif;max-width:36rem;margin:4rem auto;line-height:1.6">
<h1>{title}</h1><p>{body}</p></body></html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _LoopbackServer

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        ok, title, body = self.server.on_callback(params)
        page = _PAGE.format(title=html.escape(title), body=html.escape(body)).encode("utf-8")
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # The default handler logs the request line, which contains the authorization code.
        return


class _LoopbackServer(HTTPServer):
    def __init__(self, on_callback: Callable[[dict[str, str]], tuple[bool, str, str]]) -> None:
        super().__init__(("127.0.0.1", 0), _CallbackHandler)
        self.on_callback = on_callback


@dataclass
class LoginResult:
    status: Literal["pending", "success", "error"]
    message: str = ""


class LoginFlow:
    """One browser login attempt. Thread-safe; finishes at most once."""

    def __init__(self, broker: BrokerClient, store: TokenStore, timeout: float = 600.0) -> None:
        self._broker = broker
        self._store = store
        self._verifier, challenge = make_pkce_pair()
        self._server = _LoopbackServer(self._handle_callback)
        self.state = f"{self._server.server_address[1]}.{secrets.token_urlsafe(24)}"
        self.authorize_url = broker.authorize_url(challenge, self.state)
        self._lock = threading.Lock()
        self._done = threading.Event()
        self.result = LoginResult("pending")
        threading.Thread(target=self._server.serve_forever, name="oauth-loopback", daemon=True).start()
        self._timer = threading.Timer(timeout, self._expire)
        self._timer.daemon = True
        self._timer.start()

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    def wait(self, seconds: float) -> LoginResult:
        self._done.wait(timeout=max(0.0, seconds))
        return self.result

    def cancel(self) -> None:
        self._finish(LoginResult("error", "授權流程已取消。"))

    def _expire(self) -> None:
        self._finish(LoginResult("error", "授權流程逾時（10 分鐘內未完成）。請重新呼叫 connect_google。"))

    def _finish(self, result: LoginResult) -> bool:
        with self._lock:
            if self._done.is_set():
                return False
            self.result = result
            self._done.set()
        self._timer.cancel()
        # shutdown() blocks until serve_forever returns, so never call it on the handler thread.
        threading.Thread(target=self._stop_server, daemon=True).start()
        return True

    def _stop_server(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handle_callback(self, params: dict[str, str]) -> tuple[bool, str, str]:
        if self._done.is_set():
            return False, "這次授權已經處理過", "請回到 Claude Code。如需重新授權，請再呼叫 connect_google。"
        if not hmac.compare_digest(params.get("state", ""), self.state):
            # Not ours (stale tab or forged request): ignore without ending the flow.
            log.warning("Ignored loopback callback with mismatched state.")
            return False, "授權驗證失敗", "state 參數不符，已拒絕這次回呼。請回到 Claude Code 重新授權。"

        if "error" in params:
            reason = params["error"]
            detail = "你取消了授權。" if reason == "access_denied" else f"Google 回傳錯誤：{reason}。"
            self._finish(LoginResult("error", f"{detail}如需使用請重新呼叫 connect_google。"))
            return False, "授權未完成", detail + "請回到 Claude Code。"

        code = params.get("code")
        if not code:
            self._finish(LoginResult("error", "授權回呼缺少 code 參數。請重新呼叫 connect_google。"))
            return False, "授權未完成", "回呼缺少授權碼，請回到 Claude Code 重新授權。"

        try:
            token = StoredToken.from_token_response(self._broker.exchange_code(code, self._verifier))
        except BrokerError as exc:
            self._finish(LoginResult("error", f"交換 token 失敗：{exc}"))
            return False, "授權未完成", f"交換 token 失敗：{exc}"
        except ValueError:
            self._finish(LoginResult("error", "OAuth 後端回傳的 token 不完整（缺少 refresh token）。"))
            return False, "授權未完成", "後端回傳的 token 不完整，請回到 Claude Code 重新授權。"
        except Exception:  # noqa: BLE001 - never leave the browser hanging or the flow pending
            log.exception("Unexpected error while exchanging the authorization code.")
            self._finish(LoginResult("error", "交換 token 時發生未預期的錯誤，請重新呼叫 connect_google。"))
            return False, "授權未完成", "發生未預期的錯誤，請回到 Claude Code 重新授權。"

        if not token.has_scope(DRIVE_READONLY_SCOPE):
            self._finish(
                LoginResult(
                    "error",
                    "授權時沒有勾選 Google 雲端硬碟的讀取權限，因此無法讀取 Drive。"
                    "請重新呼叫 connect_google，並在同意畫面勾選雲端硬碟權限。",
                )
            )
            return False, "缺少 Drive 權限", "同意畫面中沒有勾選 Drive 權限。請回到 Claude Code 重新授權並勾選。"

        self._store.save(token)
        self._finish(LoginResult("success", "Google 帳號已連接。"))
        return True, "授權完成", "已連接 Google 帳號，可以關閉這個分頁，回到 Claude Code。"
