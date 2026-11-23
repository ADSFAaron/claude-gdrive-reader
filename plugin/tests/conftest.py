from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
import respx

from gdrive_reader.config import Settings
from gdrive_reader.services import Services, build_services
from gdrive_reader.token_store import StoredToken, TokenStore

BACKEND = "https://broker.test"
DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"


def make_token(expires_in: float = 3600, refresh: str = "refresh-1", access: str = "access-1") -> StoredToken:
    return StoredToken(
        access_token=access, refresh_token=refresh, expires_at=time.time() + expires_in, scope=DRIVE_READONLY
    )


class FakeTokens:
    """TokenProvider double for DriveClient tests."""

    def __init__(self) -> None:
        self.token = "access-1"
        self.refreshes = 0

    def access_token(self) -> str:
        return self.token

    def force_refresh(self) -> str:
        self.refreshes += 1
        self.token = f"access-{self.refreshes + 1}"
        return self.token


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(backend_url=BACKEND, data_dir=tmp_path / "data")


@pytest.fixture
def store(settings: Settings) -> TokenStore:
    return TokenStore(settings.token_path)


@pytest.fixture
def mock_http():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def services(settings: Settings, mock_http: respx.MockRouter) -> Services:
    opened: list[str] = []
    svc = build_services(settings, open_browser=lambda url: opened.append(url) or True)
    svc.opened_urls = opened  # type: ignore[attr-defined]
    yield svc
    svc.close()


def drive_client(tokens: FakeTokens | None = None):
    from gdrive_reader.drive import DriveClient

    return DriveClient(tokens or FakeTokens(), httpx.Client(), sleep=lambda _s: None)


def make_pdf(pages: list[str]) -> bytes:
    """Build a small valid PDF with one line of Helvetica text per page ("" = page without text)."""
    objects: list[bytes] = []
    page_ids = [4 + 2 * i for i in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        content_id = page_ids[i] + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def json_response(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)
