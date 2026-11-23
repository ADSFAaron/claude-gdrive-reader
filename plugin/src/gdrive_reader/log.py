"""Logging to stderr (stdout is reserved for the MCP protocol), with token redaction."""

from __future__ import annotations

import logging
import os
import re
import sys

_SECRET_PATTERNS = (
    re.compile(r"ya29\.[\w.-]+"),  # Google access tokens
    re.compile(r"1//[\w.-]+"),  # Google refresh tokens
    re.compile(r"(?i)((?:code|code_verifier|refresh_token|access_token|client_secret)=)[^&\s\"']+"),
)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern in _SECRET_PATTERNS:
            message = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", message)
        record.msg, record.args = message, None
        return True


def configure_logging() -> None:
    level = os.environ.get("GDRIVE_READER_LOG_LEVEL", "WARNING").upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs every request URL at INFO; keep it quiet unless debugging.
    logging.getLogger("httpx").setLevel(logging.WARNING if level != "DEBUG" else logging.DEBUG)
