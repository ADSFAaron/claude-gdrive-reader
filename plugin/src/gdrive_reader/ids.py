"""Turn whatever the user pasted (share link, URL or bare ID) into a Drive file ID."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .errors import InvalidReference

_ID = r"[A-Za-z0-9_-]{10,}"
_ID_RE = re.compile(rf"^{_ID}$")
_PATH_PATTERNS = (
    re.compile(rf"/folders/({_ID})"),
    re.compile(rf"/file/d/({_ID})"),
    re.compile(rf"/(?:document|spreadsheets|presentation|drawings|forms)/d/({_ID})"),
)


def parse_drive_id(ref: str) -> str:
    """Return the Drive ID referenced by ``ref``.

    Accepts a bare ID, ``root`` (the user's My Drive), and the URL shapes Google
    Drive and Docs produce: ``/drive/folders/<id>``, ``/file/d/<id>/view``,
    ``/document/d/<id>/edit``, ``?id=<id>``.
    """
    cleaned = ref.strip().strip("<>\"'`")
    if not cleaned:
        raise InvalidReference("沒有提供資料夾或檔案。請貼上 Google Drive 分享連結或 ID。")
    if cleaned == "root" or _ID_RE.match(cleaned):
        return cleaned

    parsed = urlparse(cleaned if "://" in cleaned else f"https://{cleaned}")
    host = (parsed.hostname or "").lower()
    if host == "google.com" or host.endswith(".google.com"):
        for pattern in _PATH_PATTERNS:
            match = pattern.search(parsed.path)
            if match:
                return match.group(1)
        ids = parse_qs(parsed.query).get("id")
        if ids and _ID_RE.match(ids[0]):
            return ids[0]

    raise InvalidReference(f"無法從「{ref}」辨識出 Google Drive 的檔案或資料夾 ID。請貼上分享連結或 ID。")
