"""Local token storage: one JSON file, readable only by the current user.

The file lives in the plugin's data directory (see ``config.py``). It is written
atomically with mode 0600 inside a 0700 directory, so a crash never leaves a
half-written file and other local users cannot read it. Tokens are never logged
or returned through MCP tools.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_FORMAT_VERSION = 1


@dataclass(frozen=True)
class StoredToken:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str

    def expires_soon(self, now: float | None = None, leeway: float = 60.0) -> bool:
        return (time.time() if now is None else now) >= self.expires_at - leeway

    def has_scope(self, scope: str) -> bool:
        return scope in self.scope.split()

    @classmethod
    def from_token_response(
        cls, data: dict[str, Any], previous: StoredToken | None = None, now: float | None = None
    ) -> StoredToken:
        """Build from a Google token response, keeping the old refresh token if none is returned."""
        refresh = data.get("refresh_token") or (previous.refresh_token if previous else "")
        if not data.get("access_token") or not refresh:
            raise ValueError("token response is missing access_token or refresh_token")
        issued = time.time() if now is None else now
        return cls(
            access_token=data["access_token"],
            refresh_token=refresh,
            expires_at=issued + float(data.get("expires_in", 3600)),
            scope=data.get("scope") or (previous.scope if previous else ""),
        )


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> StoredToken | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return StoredToken(
                access_token=raw["access_token"],
                refresh_token=raw["refresh_token"],
                expires_at=float(raw["expires_at"]),
                scope=raw.get("scope", ""),
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("Token file %s is unreadable; ignoring it.", self.path)
            return None

    def save(self, token: StoredToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir's mode only applies to a directory it creates; ${CLAUDE_PLUGIN_DATA} already
        # exists (usually 0755), so tighten it explicitly.
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            log.warning("Could not restrict permissions on %s.", self.path.parent)
        payload = json.dumps({"version": _FORMAT_VERSION, **asdict(token)}).encode("utf-8")
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def delete(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
