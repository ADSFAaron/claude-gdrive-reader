"""Command-line entry point for testing the plugin without Claude Code.

    uv run gdrive-reader login
    uv run gdrive-reader ls "https://drive.google.com/drive/folders/<id>"
    uv run gdrive-reader cat <file-id>

Outside Claude Code the token is stored in ~/.config/gdrive-reader/ unless
GDRIVE_READER_DATA_DIR says otherwise.
"""

from __future__ import annotations

import argparse
import sys

from . import tools
from .errors import GDriveReaderError
from .log import configure_logging
from .services import build_services


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gdrive-reader", description="Read-only Google Drive access.")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="connect a Google account in the browser")
    login.add_argument("--force", action="store_true", help="sign in again even if already connected")
    sub.add_parser("status", help="show the connected account")
    sub.add_parser("logout", help="revoke access and delete local tokens")
    ls = sub.add_parser("ls", help="list a folder")
    ls.add_argument("folder")
    ls.add_argument("--flat", action="store_true", help="do not descend into subfolders")
    ls.add_argument("--max-items", type=int, default=500)
    cat = sub.add_parser("cat", help="read a file")
    cat.add_argument("file")
    cat.add_argument("--offset", type=int, default=0)
    cat.add_argument("--max-chars", type=int, default=10_000)
    cat.add_argument("--start-page", type=int, default=1)
    args = parser.parse_args(argv)

    configure_logging()
    svc = build_services()
    try:
        if args.command == "login":
            print(tools.connect(svc, wait_seconds=300, force=args.force))
        elif args.command == "status":
            print(tools.status(svc))
        elif args.command == "logout":
            print(tools.disconnect(svc))
        elif args.command == "ls":
            print(tools.list_folder(svc, args.folder, recursive=not args.flat, max_items=args.max_items))
        elif args.command == "cat":
            print(
                tools.read_file(
                    svc, args.file, offset=args.offset, max_chars=args.max_chars, start_page=args.start_page
                )
            )
    except GDriveReaderError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    finally:
        svc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
