"""Session management: login, token refresh, logout."""

from __future__ import annotations

import logging
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from .errors import BrokerError, NotAuthenticated, ReauthRequired
from .oauth import BrokerClient, LoginFlow, LoginResult
from .token_store import StoredToken, TokenStore

log = logging.getLogger(__name__)

GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


@dataclass
class ConnectOutcome:
    result: LoginResult
    authorize_url: str
    browser_opened: bool


class AuthManager:
    """Owns the stored token. Safe to call from several threads."""

    def __init__(
        self,
        broker: BrokerClient,
        store: TokenStore,
        http: httpx.Client,
        open_browser: Callable[[str], bool] = webbrowser.open,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._broker = broker
        self._store = store
        self._http = http
        self._open_browser = open_browser
        self._clock = clock
        self._lock = threading.Lock()
        self._flow: LoginFlow | None = None

    # -- login -----------------------------------------------------------------

    def connect(self, wait_seconds: float) -> ConnectOutcome:
        """Start (or resume) a browser login and wait up to ``wait_seconds`` for it."""
        with self._lock:
            flow = self._flow
            if flow is None or flow.finished:
                flow = self._flow = LoginFlow(self._broker, self._store)
                opened = self._try_open_browser(flow.authorize_url)
            else:
                opened = False  # a login is already waiting in an open tab
        return ConnectOutcome(flow.wait(wait_seconds), flow.authorize_url, opened)

    def pending_login(self) -> LoginFlow | None:
        flow = self._flow
        return flow if flow is not None and not flow.finished else None

    def _try_open_browser(self, url: str) -> bool:
        try:
            return bool(self._open_browser(url))
        except Exception:  # noqa: BLE001 - no browser (SSH, headless) is a normal case
            log.info("Could not open a browser; the user will open the URL manually.")
            return False

    # -- tokens ------------------------------------------------------------------

    def stored_token(self) -> StoredToken | None:
        return self._store.load()

    def access_token(self) -> str:
        with self._lock:
            token = self._store.load()
            if token is None:
                raise NotAuthenticated()
            if token.expires_soon(now=self._clock()):
                token = self._refresh(token)
            return token.access_token

    def force_refresh(self) -> str:
        """Refresh even if the token looks valid (after a 401 from Google)."""
        with self._lock:
            token = self._store.load()
            if token is None:
                raise NotAuthenticated()
            return self._refresh(token).access_token

    def _refresh(self, token: StoredToken) -> StoredToken:
        try:
            data = self._broker.refresh(token.refresh_token)
        except BrokerError as exc:
            if exc.code == "invalid_grant":
                self._store.delete()
                raise ReauthRequired() from exc
            raise
        new_token = StoredToken.from_token_response(data, previous=token, now=self._clock())
        self._store.save(new_token)
        return new_token

    # -- logout ------------------------------------------------------------------

    def disconnect(self) -> tuple[bool, bool]:
        """Revoke the grant at Google and delete local tokens.

        Returns ``(had_token, revoked)``. Local data is deleted even if revocation fails.
        """
        with self._lock:
            if self._flow is not None:
                self._flow.cancel()
                self._flow = None
            token = self._store.load()
            revoked = False
            if token is not None:
                try:
                    resp = self._http.post(GOOGLE_REVOKE_URL, data={"token": token.refresh_token})
                    revoked = resp.status_code == 200
                except httpx.HTTPError:
                    log.warning("Token revocation request failed; deleting local token anyway.")
            had_token = self._store.delete()
            return had_token, revoked
