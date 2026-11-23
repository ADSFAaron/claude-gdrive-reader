from __future__ import annotations

import os
import stat

import pytest

from conftest import make_token
from gdrive_reader.config import load_settings
from gdrive_reader.errors import ConfigError, InvalidReference
from gdrive_reader.ids import parse_drive_id
from gdrive_reader.token_store import StoredToken, TokenStore

FOLDER_ID = "1FakeFolderIdForTests_abcdefXYZ"  # synthetic ID, not a real folder


@pytest.mark.parametrize(
    "ref",
    [
        FOLDER_ID,
        f"  {FOLDER_ID}  ",
        f"https://drive.google.com/drive/folders/{FOLDER_ID}?usp=drive_link",
        f"https://drive.google.com/drive/u/1/folders/{FOLDER_ID}",
        f"drive.google.com/drive/folders/{FOLDER_ID}",
        f"https://drive.google.com/file/d/{FOLDER_ID}/view?usp=sharing",
        f"https://docs.google.com/document/d/{FOLDER_ID}/edit",
        f"https://drive.google.com/open?id={FOLDER_ID}",
        f"<https://drive.google.com/drive/folders/{FOLDER_ID}>",
    ],
)
def test_parse_drive_id_accepts_links_and_ids(ref: str) -> None:
    assert parse_drive_id(ref) == FOLDER_ID


@pytest.mark.parametrize("ref", ["", "hello", "https://example.com/drive/folders/1FakeFolderIdForTests", "../../etc"])
def test_parse_drive_id_rejects_garbage(ref: str) -> None:
    with pytest.raises(InvalidReference):
        parse_drive_id(ref)


def test_token_store_round_trip_with_private_permissions(store: TokenStore) -> None:
    token = make_token()
    store.save(token)
    assert store.load() == token
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700
    assert store.delete() is True
    assert store.load() is None
    assert store.delete() is False


def test_token_store_tightens_existing_directory(tmp_path) -> None:
    data_dir = tmp_path / "plugin-data"
    data_dir.mkdir(mode=0o755)
    os.chmod(data_dir, 0o755)  # noqa: S103 - simulates ${CLAUDE_PLUGIN_DATA} as Claude Code creates it
    TokenStore(data_dir / "token.json").save(make_token())
    assert stat.S_IMODE(os.stat(data_dir).st_mode) == 0o700


def test_token_store_ignores_corrupt_file(store: TokenStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json")
    assert store.load() is None


def test_refresh_response_keeps_previous_refresh_token() -> None:
    previous = make_token(refresh="keep-me")
    new = StoredToken.from_token_response({"access_token": "a2", "expires_in": 3599}, previous=previous, now=1000)
    assert new.refresh_token == "keep-me"
    assert new.expires_at == 1000 + 3599
    assert new.scope == previous.scope


def test_settings_fall_back_when_plugin_data_placeholder_is_unexpanded(tmp_path) -> None:
    env = {"GDRIVE_READER_DATA_DIR": "${CLAUDE_PLUGIN_DATA}", "XDG_CONFIG_HOME": str(tmp_path)}
    assert load_settings(env).data_dir == tmp_path / "gdrive-reader"


def test_settings_require_https_backend(tmp_path) -> None:
    assert load_settings({"GDRIVE_READER_BACKEND_URL": "http://localhost:8787/"}).require_backend() == (
        "http://localhost:8787"
    )
    with pytest.raises(ConfigError):
        load_settings({"GDRIVE_READER_BACKEND_URL": "http://evil.example"}).require_backend()
    assert load_settings({}).require_backend().startswith("https://")  # deployed default
    with pytest.raises(ConfigError):
        load_settings({"GDRIVE_READER_BACKEND_URL": "https://x.REPLACE-ME.workers.dev"}).require_backend()
