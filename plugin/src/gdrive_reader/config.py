"""Runtime configuration, read from environment variables.

Inside Claude Code the plugin's ``.mcp.json`` sets ``GDRIVE_READER_DATA_DIR`` to
``${CLAUDE_PLUGIN_DATA}``, a per-plugin directory that survives plugin updates
and is removed when the plugin is uninstalled.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .errors import ConfigError

# OAuth backend deployed for this repository (Cloudflare Worker in ../backend).
# Anyone running their own copy overrides it with GDRIVE_READER_BACKEND_URL.
DEFAULT_BACKEND_URL = "https://gdrive-reader-auth.adsfaaron.workers.dev"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class Settings:
    backend_url: str
    data_dir: Path
    http_timeout: float = 30.0

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token.json"

    def require_backend(self) -> str:
        """Return the backend URL, or explain why it cannot be used."""
        if "REPLACE-ME" in self.backend_url:
            raise ConfigError(
                "尚未設定 OAuth 後端網址。請在 plugin/src/gdrive_reader/config.py 設定 DEFAULT_BACKEND_URL，"
                "或設定環境變數 GDRIVE_READER_BACKEND_URL。"
            )
        parsed = urlparse(self.backend_url)
        if parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS):
            return self.backend_url
        raise ConfigError(f"OAuth 後端網址必須是 https（本機開發可用 http://localhost）：{self.backend_url}")


def _default_data_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "gdrive-reader"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    backend = env.get("GDRIVE_READER_BACKEND_URL", "").strip() or DEFAULT_BACKEND_URL
    raw_dir = env.get("GDRIVE_READER_DATA_DIR", "").strip()
    # An unexpanded "${CLAUDE_PLUGIN_DATA}" means we were started outside Claude Code.
    data_dir = Path(raw_dir).expanduser() if raw_dir and "${" not in raw_dir else _default_data_dir(env)
    return Settings(backend_url=backend.rstrip("/"), data_dir=data_dir)
