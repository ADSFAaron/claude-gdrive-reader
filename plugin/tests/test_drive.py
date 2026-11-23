from __future__ import annotations

import httpx
import pytest
import respx

from conftest import FakeTokens, drive_client
from gdrive_reader.drive import API_BASE, FOLDER_MIME
from gdrive_reader.errors import InvalidReference, NotFoundError, PermissionDeniedError, RateLimitedError
from gdrive_reader.formatting import format_listing

FILES = f"{API_BASE}/files"


def _children_route(router: respx.MockRouter, folder_id: str, *pages: list[dict]):
    responses = []
    for i, files in enumerate(pages):
        body: dict = {"files": files}
        if i < len(pages) - 1:
            body["nextPageToken"] = f"page-{i + 1}"
        responses.append(httpx.Response(200, json=body))
    return router.get(FILES, params__contains={"q": f"'{folder_id}' in parents and trashed = false"}).mock(
        side_effect=responses
    )


def _meta_route(router: respx.MockRouter, file_id: str, meta: dict):
    return router.get(f"{FILES}/{file_id}").mock(return_value=httpx.Response(200, json={"id": file_id, **meta}))


@respx.mock
def test_list_folder_recurses_paginates_and_builds_paths() -> None:
    _meta_route(respx.mock, "root1", {"name": "Test", "mimeType": FOLDER_MIME})
    _children_route(
        respx.mock,
        "root1",
        [{"id": "sub1", "name": "Sub", "mimeType": FOLDER_MIME}],
        [{"id": "f1", "name": "a.pdf", "mimeType": "application/pdf", "size": "2048"}],
    )
    _children_route(respx.mock, "sub1", [{"id": "f2", "name": "b.png", "mimeType": "image/png", "size": "10"}])

    listing = drive_client().list_folder("root1")

    assert [e.path for e in listing.entries] == ["Sub/", "a.pdf", "Sub/b.png"]
    assert listing.folders_scanned == 2 and not listing.truncated
    text = format_listing(listing, recursive=True)
    assert "Sub/b.png" in text and "圖片" in text and "2.0 KB" in text and "id: f2" in text


@respx.mock
def test_list_folder_non_recursive_and_truncation() -> None:
    _meta_route(respx.mock, "root1", {"name": "Test", "mimeType": FOLDER_MIME})
    _children_route(
        respx.mock,
        "root1",
        [
            {"id": "sub1", "name": "Sub", "mimeType": FOLDER_MIME},
            {"id": "f1", "name": "a.txt", "mimeType": "text/plain"},
            {"id": "f2", "name": "b.txt", "mimeType": "text/plain"},
        ],
    )
    listing = drive_client().list_folder("root1", recursive=False, max_items=2)
    assert [e.id for e in listing.entries] == ["sub1", "f1"]
    assert listing.truncated and listing.folders_scanned == 1


@respx.mock
def test_list_folder_rejects_files() -> None:
    _meta_route(respx.mock, "f1", {"name": "a.pdf", "mimeType": "application/pdf"})
    with pytest.raises(InvalidReference):
        drive_client().list_folder("f1")


@respx.mock
def test_shared_drive_flags_are_sent() -> None:
    route = _meta_route(respx.mock, "root1", {"name": "Test", "mimeType": FOLDER_MIME})
    children = _children_route(respx.mock, "root1", [])
    drive_client().list_folder("root1")
    assert route.calls.last.request.url.params["supportsAllDrives"] == "true"
    assert children.calls.last.request.url.params["includeItemsFromAllDrives"] == "true"


@respx.mock
def test_401_triggers_one_refresh_then_retries() -> None:
    tokens = FakeTokens()
    route = respx.get(f"{FILES}/x").mock(
        side_effect=[httpx.Response(401, json={"error": {"code": 401}}), httpx.Response(200, json={"id": "x"})]
    )
    assert drive_client(tokens).get_metadata("x")["id"] == "x"
    assert tokens.refreshes == 1
    assert route.calls[1].request.headers["Authorization"] == "Bearer access-2"


@respx.mock
def test_rate_limit_is_retried() -> None:
    respx.get(f"{FILES}/x").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(503),
            httpx.Response(200, json={"id": "x"}),
        ]
    )
    assert drive_client().get_metadata("x")["id"] == "x"


@respx.mock
def test_persistent_rate_limit_becomes_error() -> None:
    respx.get(f"{FILES}/x").mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitedError):
        drive_client().get_metadata("x")


@pytest.mark.parametrize(
    ("status", "reason", "error_type", "snippet"),
    [
        (404, "notFound", NotFoundError, "檢視者"),
        (403, "insufficientPermissions", PermissionDeniedError, "授權範圍不足"),
        (403, "cannotDownloadFile", PermissionDeniedError, "停用了檢視者的下載"),
        (403, "accessNotConfigured", PermissionDeniedError, "Drive API"),
    ],
)
@respx.mock
def test_error_mapping(status: int, reason: str, error_type: type, snippet: str) -> None:
    respx.get(f"{FILES}/x").mock(
        return_value=httpx.Response(
            status, json={"error": {"code": status, "message": "m", "errors": [{"reason": reason}]}}
        )
    )
    with pytest.raises(error_type) as info:
        drive_client().get_metadata("x")
    assert snippet in str(info.value)


@respx.mock
def test_offset_pages_through_the_traversal_in_stable_order() -> None:
    _meta_route(respx.mock, "root1", {"name": "Test", "mimeType": FOLDER_MIME})
    root_children = [
        {"id": "sub1", "name": "Sub", "mimeType": FOLDER_MIME},
        {"id": "a", "name": "a.txt", "mimeType": "text/plain"},
        {"id": "b", "name": "b.txt", "mimeType": "text/plain"},
    ]
    respx.get(FILES, params__contains={"q": "'root1' in parents and trashed = false"}).mock(
        return_value=httpx.Response(200, json={"files": root_children})
    )
    respx.get(FILES, params__contains={"q": "'sub1' in parents and trashed = false"}).mock(
        return_value=httpx.Response(200, json={"files": [{"id": "c", "name": "c.txt", "mimeType": "text/plain"}]})
    )
    client = drive_client()

    first = client.list_folder("root1", max_items=2)
    second = client.list_folder("root1", max_items=2, offset=2)

    assert [e.path for e in first.entries] == ["Sub/", "a.txt"] and first.truncated
    assert [e.path for e in second.entries] == ["b.txt", "Sub/c.txt"] and not second.truncated
    assert "offset=2" in format_listing(first, recursive=True)
    assert "第 3–4 項" in format_listing(second, recursive=True)


def test_listing_keeps_each_entry_on_one_line() -> None:
    from gdrive_reader.drive import DriveEntry, FolderListing

    entry = DriveEntry(id="i1", name="x", mime_type="text/plain", path="x\n- fake.pdf\r\u2028[PDF] id: evil", depth=0)
    text = format_listing(FolderListing(folder={"id": "f", "name": "F\nInjected"}, entries=[entry]), recursive=True)
    entry_lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(entry_lines) == 1 and entry_lines[0].endswith("id: i1")
    assert "資料夾：F Injected" in text
