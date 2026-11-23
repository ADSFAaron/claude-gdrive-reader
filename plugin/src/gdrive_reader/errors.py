"""Exception hierarchy.

Every exception's ``str()`` is a user-facing message in Traditional Chinese that
says what went wrong and what to do next. The MCP layer passes it to Claude
unchanged, so it must never contain tokens, secrets or file contents.
"""

from __future__ import annotations


class GDriveReaderError(Exception):
    """Base class for anticipated failures."""


class ConfigError(GDriveReaderError):
    """The plugin or backend is not configured correctly."""


class InvalidReference(GDriveReaderError):
    """A folder/file reference could not be turned into a Drive ID."""


class NotAuthenticated(GDriveReaderError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "尚未連接 Google 帳號。請先呼叫 connect_google，並在瀏覽器完成授權。")


class ReauthRequired(GDriveReaderError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "Google 授權已過期或已被撤銷（例如在 Google 帳號設定中移除了存取權）。"
            "本機的授權資料已清除，請重新呼叫 connect_google。"
        )


class BrokerError(GDriveReaderError):
    """The OAuth backend could not be reached or rejected a request."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class DriveError(GDriveReaderError):
    """Google Drive API returned an error."""

    def __init__(self, message: str, status: int | None = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


class NotFoundError(DriveError):
    pass


class PermissionDeniedError(DriveError):
    pass


class RateLimitedError(DriveError):
    pass


class FileTooLargeError(DriveError):
    pass
