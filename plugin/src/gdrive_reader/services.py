"""Object wiring shared by the MCP server and the CLI."""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from . import __version__
from .auth import AuthManager
from .config import Settings, load_settings
from .drive import DriveClient
from .oauth import BrokerClient
from .token_store import TokenStore


@dataclass
class Services:
    settings: Settings
    http: httpx.Client
    auth: AuthManager
    drive: DriveClient

    def close(self) -> None:
        self.http.close()


def build_services(
    settings: Settings | None = None,
    *,
    open_browser: Callable[[str], bool] = webbrowser.open,
    transport: httpx.BaseTransport | None = None,
) -> Services:
    settings = settings or load_settings()
    http = httpx.Client(
        timeout=httpx.Timeout(settings.http_timeout, connect=10.0),
        headers={"User-Agent": f"gdrive-reader/{__version__}"},
        follow_redirects=True,  # httpx drops the Authorization header on cross-origin redirects
        transport=transport,
    )
    auth = AuthManager(
        broker=BrokerClient(settings.backend_url, http),
        store=TokenStore(settings.token_path),
        http=http,
        open_browser=open_browser,
    )
    drive = DriveClient(auth, http)
    auth.on_account_change = drive.clear_cache
    return Services(settings=settings, http=http, auth=auth, drive=drive)
