"""Minimal Google Drive API v3 client (read-only).

Plain REST over httpx instead of google-api-python-client: the plugin needs five
endpoints, and a small dependency tree keeps the first ``uv run`` inside
Claude Code's MCP startup window. Every request sets ``supportsAllDrives`` so
folders that live in shared drives work too.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .errors import (
    DriveError,
    FileTooLargeError,
    GDriveReaderError,
    InvalidReference,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    ReauthRequired,
)

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,webViewLink,"
    "shortcutDetails(targetId,targetMimeType),imageMediaMetadata(width,height),capabilities(canDownload)"
)
CHILD_FIELDS = "id,name,mimeType,size,modifiedTime,shortcutDetails(targetId,targetMimeType)"

_RETRYABLE_403 = {"rateLimitExceeded", "userRateLimitExceeded"}
_SCOPE_403 = {"insufficientPermissions", "insufficientScopes", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}


class TokenProvider(Protocol):
    def access_token(self) -> str: ...
    def force_refresh(self) -> str: ...


@dataclass
class DriveEntry:
    id: str
    name: str
    mime_type: str
    path: str
    depth: int
    size: int | None = None
    modified: str | None = None
    shortcut_target: str | None = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME


@dataclass
class FolderListing:
    folder: dict[str, Any]
    entries: list[DriveEntry] = field(default_factory=list)
    offset: int = 0
    folders_scanned: int = 0
    truncated: bool = False
    depth_limited: bool = False


class _ByteCache:
    """Small in-memory LRU for downloaded file bytes, keyed by file ID and modifiedTime.

    Reading a long PDF page by page, or a long text by offset, would otherwise download
    and parse the whole file again for every call. Keying on modifiedTime means an edited
    file is fetched again.
    """

    def __init__(self, max_bytes: int = 64 * 1024 * 1024) -> None:
        self._max_bytes = max_bytes
        self._items: OrderedDict[tuple[str, ...], bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: tuple[str, ...]) -> bytes | None:
        with self._lock:
            data = self._items.get(key)
            if data is not None:
                self._items.move_to_end(key)
            return data

    def put(self, key: tuple[str, ...], data: bytes) -> None:
        if len(data) > self._max_bytes:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._size -= len(old)
            self._items[key] = data
            self._size += len(data)
            while self._size > self._max_bytes:
                _, evicted = self._items.popitem(last=False)
                self._size -= len(evicted)


class DriveClient:
    def __init__(
        self,
        tokens: TokenProvider,
        http: httpx.Client,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._tokens = tokens
        self._http = http
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._cache = _ByteCache()

    # -- public API --------------------------------------------------------------

    def about(self) -> dict[str, Any]:
        return self._get_json("/about", {"fields": "user(displayName,emailAddress)"})

    def get_metadata(self, file_id: str) -> dict[str, Any]:
        return self._get_json(f"/files/{file_id}", {"fields": FILE_FIELDS, "supportsAllDrives": "true"})

    def iter_children(self, folder_id: str) -> Iterator[dict[str, Any]]:
        params: dict[str, str] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": f"nextPageToken,files({CHILD_FIELDS})",
            "pageSize": "1000",
            "orderBy": "folder,name",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        while True:
            page = self._get_json("/files", params)
            yield from page.get("files", [])
            token = page.get("nextPageToken")
            if not token:
                return
            params = {**params, "pageToken": token}

    def list_folder(
        self, folder_id: str, recursive: bool = True, max_items: int = 200, max_depth: int = 20, offset: int = 0
    ) -> FolderListing:
        """Breadth-first listing. ``offset``/``max_items`` page through the traversal order,
        which is stable because children are sorted (folders first, then by name)."""
        folder = self.get_metadata(folder_id)
        if folder.get("mimeType") == SHORTCUT_MIME:
            target = folder.get("shortcutDetails") or {}
            if target.get("targetMimeType") == FOLDER_MIME:
                folder = self.get_metadata(target["targetId"])
        if folder.get("mimeType") != FOLDER_MIME:
            raise InvalidReference(f"「{folder.get('name', folder_id)}」不是資料夾。要讀取檔案內容請改用 read_file。")

        listing = FolderListing(folder=folder, offset=offset)
        queue: deque[tuple[str, str, int]] = deque([(folder["id"], "", 0)])
        visited = {folder["id"]}
        position = 0
        while queue:
            current_id, prefix, depth = queue.popleft()
            listing.folders_scanned += 1
            for item in self.iter_children(current_id):
                if position >= offset and len(listing.entries) >= max_items:
                    listing.truncated = True
                    return listing
                entry = _to_entry(item, prefix, depth)
                if entry.is_folder and recursive and entry.id not in visited:
                    if depth + 1 >= max_depth:
                        listing.depth_limited = True
                    else:
                        visited.add(entry.id)
                        queue.append((entry.id, entry.path, depth + 1))
                if position >= offset:
                    listing.entries.append(entry)
                position += 1
        return listing

    def download(self, file_id: str, max_bytes: int, version: str | None = None) -> bytes:
        """Download file content. Pass ``version`` (modifiedTime) to reuse a cached copy."""
        key = ("media", file_id, version) if version else None
        cached = self._cache.get(key) if key else None
        if cached is not None:
            return cached
        data = self._get_bytes(f"/files/{file_id}", {"alt": "media", "supportsAllDrives": "true"}, max_bytes)
        if key:
            self._cache.put(key, data)
        return data

    def export(self, file_id: str, mime_type: str, max_bytes: int, version: str | None = None) -> bytes:
        key = ("export", file_id, mime_type, version) if version else None
        cached = self._cache.get(key) if key else None
        if cached is not None:
            return cached
        data = self._get_bytes(f"/files/{file_id}/export", {"mimeType": mime_type}, max_bytes)
        if key:
            self._cache.put(key, data)
        return data

    def probe(self, file_id: str, max_bytes: int = 64 * 1024) -> int:
        """Read up to ``max_bytes`` of a file's content to prove it is downloadable.

        Returns the number of bytes actually read. Sends a Range header and also stops
        reading on its own, so it never pulls a whole large file.
        """
        resp = self._send(
            f"/files/{file_id}",
            {"alt": "media", "supportsAllDrives": "true"},
            stream=True,
            headers={"Range": f"bytes=0-{max_bytes - 1}"},
        )
        total = 0
        try:
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total >= max_bytes:
                    break
        finally:
            resp.close()
        return min(total, max_bytes)

    # -- transport ---------------------------------------------------------------

    def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        resp = self._send(path, params, stream=False)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise DriveError("Google Drive 回傳了無法解析的回應，請稍後再試。", status=resp.status_code)
        return data

    def _get_bytes(self, path: str, params: dict[str, str], max_bytes: int) -> bytes:
        resp = self._send(path, params, stream=True)
        try:
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise FileTooLargeError(
                        f"檔案超過 {max_bytes // (1024 * 1024)} MB 的讀取上限，已停止下載。", status=resp.status_code
                    )
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            resp.close()

    def _send(
        self, path: str, params: dict[str, str], stream: bool, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            request = self._http.build_request(
                "GET",
                f"{API_BASE}{path}",
                params=params,
                headers={**(headers or {}), "Authorization": f"Bearer {self._tokens.access_token()}"},
            )
            try:
                resp = self._http.send(request, stream=stream)
            except httpx.TimeoutException as exc:
                if attempt < self._max_attempts:
                    self._backoff(attempt, None)
                    continue
                raise DriveError("連線 Google Drive 逾時，請稍後再試。") from exc
            except httpx.HTTPError as exc:
                if attempt < self._max_attempts:
                    self._backoff(attempt, None)
                    continue
                raise DriveError("無法連線到 Google Drive，請檢查網路連線。") from exc

            if resp.is_success:
                return resp

            if stream:
                resp.read()
            resp.close()
            status = resp.status_code
            reason, message = _error_details(resp)

            if status == 401 and not refreshed:
                self._tokens.force_refresh()
                refreshed = True
                continue
            retryable = status == 429 or status >= 500 or (status == 403 and reason in _RETRYABLE_403)
            if retryable and attempt < self._max_attempts:
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            raise _map_error(status, reason, message)

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = min(2.0 ** (attempt - 1), 8.0)
        if retry_after and retry_after.isdigit():
            delay = min(float(retry_after), 30.0)
        self._sleep(delay)


def _to_entry(item: dict[str, Any], prefix: str, depth: int) -> DriveEntry:
    mime = item.get("mimeType", "")
    name = item.get("name", item["id"])
    size = item.get("size")
    shortcut = item.get("shortcutDetails") or {}
    return DriveEntry(
        id=item["id"],
        name=name,
        mime_type=mime,
        path=f"{prefix}{name}/" if mime == FOLDER_MIME else f"{prefix}{name}",
        depth=depth,
        size=int(size) if size is not None else None,
        modified=item.get("modifiedTime"),
        shortcut_target=shortcut.get("targetId"),
    )


def _error_details(resp: httpx.Response) -> tuple[str | None, str]:
    try:
        error = resp.json().get("error", {})
    except ValueError:
        return None, resp.text[:200]
    if not isinstance(error, dict):
        return None, str(error)[:200]
    reasons = [e.get("reason") for e in error.get("errors", []) if isinstance(e, dict)]
    details = [d.get("reason") for d in error.get("details", []) if isinstance(d, dict)]
    reason = next((r for r in reasons + details if r), None)
    return reason, str(error.get("message", ""))[:200]


def _map_error(status: int, reason: str | None, message: str) -> GDriveReaderError:
    if status == 401:
        return ReauthRequired("Google 拒絕了目前的授權（HTTP 401）。請呼叫 connect_google 重新授權。")
    if status == 404:
        return NotFoundError(
            "找不到這個檔案或資料夾，或目前登入的 Google 帳號沒有存取權。"
            "請確認連結正確，並確認這個帳號已被分享至少「檢視者」權限。"
            "若你提供的是檔名而不是 ID，請先用 list_folder 列出所在資料夾，再用列出的 id 讀取。",
            status,
            reason,
        )
    if status == 403:
        if reason in _SCOPE_403:
            return PermissionDeniedError(
                "目前的授權範圍不足以讀取 Drive。請呼叫 disconnect_google 後重新 connect_google，並勾選 Drive 權限。",
                status,
                reason,
            )
        if reason == "cannotDownloadFile":
            return PermissionDeniedError(
                "檔案擁有者停用了檢視者的下載權限，因此無法讀取內容（但仍可列出檔名）。", status, reason
            )
        if reason in {"accessNotConfigured", "SERVICE_DISABLED"}:
            return PermissionDeniedError(
                "OAuth Client 所屬的 Google Cloud 專案尚未啟用 Google Drive API。", status, reason
            )
        if reason == "exportSizeLimitExceeded":
            return FileTooLargeError("這份 Google 文件太大，超過 Google 的匯出上限（10 MB）。", status, reason)
        if reason in _RETRYABLE_403:
            return RateLimitedError("Google Drive 請求過於頻繁，請稍後再試。", status, reason)
        return PermissionDeniedError(f"沒有權限存取這個項目（Google 回應：{message or reason}）。", status, reason)
    if status == 429:
        return RateLimitedError("Google Drive 請求過於頻繁，請稍後再試。", status, reason)
    if status >= 500:
        return DriveError(f"Google Drive 暫時無法回應（HTTP {status}），請稍後再試。", status, reason)
    return DriveError(f"Google Drive 回傳錯誤（HTTP {status}：{message or reason}）。", status, reason)
