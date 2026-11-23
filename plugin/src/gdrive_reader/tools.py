"""Tool implementations shared by the MCP server and the CLI.

Each function returns the text shown to Claude, or raises ``GDriveReaderError``
whose message explains what happened and what to do next.
"""

from __future__ import annotations

import datetime as dt

from .errors import GDriveReaderError, NotAuthenticated, ReauthRequired
from .formatting import format_listing
from .ids import parse_drive_id
from .oauth import DRIVE_READONLY_SCOPE
from .reader import read_file as _read_file
from .services import Services


def _account_label(svc: Services) -> str:
    user = svc.drive.about().get("user", {})
    email, name = user.get("emailAddress"), user.get("displayName")
    return f"{name}（{email}）" if name and email else email or name or "（無法取得帳號名稱）"


def connect(svc: Services, wait_seconds: int = 120, force: bool = False) -> str:
    if not force and svc.auth.stored_token() is not None:
        try:
            return (
                f"已經連接 Google 帳號：{_account_label(svc)}，不需要重新授權。若要切換帳號，請用 force=true 重新授權。"
            )
        except (NotAuthenticated, ReauthRequired):
            pass  # stored token no longer works; fall through to a fresh login

    svc.settings.require_backend()
    outcome = svc.auth.connect(wait_seconds)
    if outcome.result.status == "success":
        return f"已連接 Google 帳號：{_account_label(svc)}（權限：Google Drive 唯讀）。現在可以列出或讀取檔案。"
    if outcome.result.status == "error":
        raise GDriveReaderError(outcome.result.message)

    opened = "已在瀏覽器開啟 Google 授權頁面。" if outcome.browser_opened else "無法自動開啟瀏覽器。"
    return (
        f"{opened}請使用者在瀏覽器登入 Google，並勾選 Google 雲端硬碟的讀取權限。\n"
        f"如果頁面沒有出現，請手動開啟這個網址：\n{outcome.authorize_url}\n"
        "授權頁面在 10 分鐘內有效。完成後呼叫 auth_status 確認，或再次呼叫 connect_google 繼續等待。"
    )


def status(svc: Services) -> str:
    token = svc.auth.stored_token()
    pending = svc.auth.pending_login()
    if token is None:
        message = "尚未連接 Google 帳號。"
        if pending is not None:
            message += f"\n有一個授權流程正在等待使用者在瀏覽器完成：\n{pending.authorize_url}"
        else:
            message += "請呼叫 connect_google 開始授權。"
        return message

    try:
        account = _account_label(svc)
    except ReauthRequired as exc:
        return str(exc)
    current = svc.auth.stored_token() or token
    expires = dt.datetime.fromtimestamp(current.expires_at).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    scope = "Google Drive 唯讀" if current.has_scope(DRIVE_READONLY_SCOPE) else current.scope
    return (
        f"已連接 Google 帳號：{account}\n"
        f"授權範圍：{scope}\n"
        f"目前的 access token 有效至 {expires}（到期前會自動更新）。\n"
        f"授權資料存放於：{svc.settings.token_path}（權限 0600）"
    )


def disconnect(svc: Services) -> str:
    had_token, revoked = svc.auth.disconnect()
    if not had_token:
        return "目前沒有已連接的 Google 帳號，本機也沒有授權資料。"
    if revoked:
        return "已向 Google 撤銷授權，並刪除本機的授權資料。"
    return (
        "已刪除本機的授權資料，但向 Google 撤銷授權時失敗（可能是網路問題或授權已失效）。"
        "如需確認，可到 https://myaccount.google.com/permissions 手動移除這個應用程式。"
    )


def list_folder(svc: Services, folder: str, recursive: bool = True, max_items: int = 200, offset: int = 0) -> str:
    folder_id = parse_drive_id(folder)
    listing = svc.drive.list_folder(folder_id, recursive=recursive, max_items=max_items, offset=offset)
    return format_listing(listing, recursive)


def read_file(
    svc: Services,
    file: str,
    offset: int = 0,
    max_chars: int = 10_000,
    start_page: int = 1,
    max_pages: int = 50,
) -> str:
    file_id = parse_drive_id(file)
    return _read_file(
        svc.drive, file_id, offset=offset, max_chars=max_chars, start_page=start_page, max_pages=max_pages
    )
