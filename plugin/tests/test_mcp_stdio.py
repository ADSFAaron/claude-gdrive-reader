"""End-to-end check of the MCP stdio protocol: the server starts, lists its tools,
and returns a readable error (not a crash) when no Google account is connected."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def test_stdio_server_lists_tools_and_reports_missing_auth(tmp_path) -> None:
    env = {
        **os.environ,
        "GDRIVE_READER_DATA_DIR": str(tmp_path),
        "GDRIVE_READER_BACKEND_URL": "https://broker.test",
    }
    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "gdrive_reader.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )

    def call(message: dict) -> dict | None:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline()) if "id" in message else None

    try:
        init = call(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        assert init["result"]["serverInfo"]["name"] == "gdrive-reader"
        call({"jsonrpc": "2.0", "method": "notifications/initialized"})

        listed = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert names == {"connect_google", "auth_status", "disconnect_google", "list_folder", "read_file"}

        result = call(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"file": "1FakeFolderIdForTests_abcdefXYZ"}},
            }
        )["result"]
        assert result["isError"] is True
        assert "connect_google" in result["content"][0]["text"]
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
        stderr = proc.stderr.read()
    assert "Traceback" not in stderr
