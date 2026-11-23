"""Plain-text rendering of tool results (read by Claude, then relayed to the user)."""

from __future__ import annotations

from .drive import FOLDER_MIME, SHORTCUT_MIME, FolderListing

_TYPE_LABELS = {
    FOLDER_MIME: "資料夾",
    SHORTCUT_MIME: "捷徑",
    "application/pdf": "PDF",
    "application/vnd.google-apps.document": "Google 文件",
    "application/vnd.google-apps.spreadsheet": "Google 試算表",
    "application/vnd.google-apps.presentation": "Google 簡報",
}


def type_label(mime_type: str) -> str:
    if mime_type in _TYPE_LABELS:
        return _TYPE_LABELS[mime_type]
    if mime_type.startswith("image/"):
        return "圖片"
    if mime_type.startswith("text/"):
        return "文字檔"
    return mime_type or "未知類型"


def human_size(size: int | None) -> str:
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_listing(listing: FolderListing, recursive: bool) -> str:
    folder = listing.folder
    files = [e for e in listing.entries if not e.is_folder]
    folders = [e for e in listing.entries if e.is_folder]
    lines = [
        f"資料夾：{folder.get('name')}（ID：{folder.get('id')}）",
        f"共 {len(listing.entries)} 個項目：{len(files)} 個檔案、{len(folders)} 個子資料夾"
        + (f"（已遞迴掃描 {listing.folders_scanned} 個資料夾）" if recursive else "（只列出第一層）"),
    ]
    if not listing.entries:
        lines.append("這個資料夾是空的（或目前帳號看不到其中任何項目）。")
    lines.append("")
    for entry in listing.entries:
        detail = type_label(entry.mime_type)
        if not entry.is_folder:
            detail += f"，{human_size(entry.size)}"
        if entry.shortcut_target:
            detail += f"，指向 ID {entry.shortcut_target}（未展開）"
        lines.append(f"- {entry.path}　[{detail}]　id: {entry.id}")
    if listing.truncated:
        lines.append(f"\n（結果已截斷：只顯示前 {len(listing.entries)} 個項目。可提高 max_items 取得更多。）")
    if listing.depth_limited:
        lines.append("（部分子資料夾超過深度上限，未繼續展開。）")
    lines.append("\n要讀取內容時，請用 read_file 並傳入上面的 id。")
    return "\n".join(lines)
