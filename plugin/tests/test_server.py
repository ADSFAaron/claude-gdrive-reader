"""Tool boundary: every failure reaches Claude as a readable message."""

from __future__ import annotations

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from gdrive_reader.server import create_server


class _Exploding:
    def __getattr__(self, name: str):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def _call(tool: str, arguments: dict) -> str:
    server = create_server(factory=lambda: _Exploding())  # type: ignore[arg-type, return-value]

    async def run() -> str:
        try:
            result = await server.call_tool(tool, arguments)
        except ToolError as exc:
            return str(exc)
        return str(result)

    return anyio.run(run)


@pytest.mark.parametrize(("tool", "arguments"), [("auth_status", {}), ("list_folder", {"folder": "root"})])
def test_unexpected_exceptions_become_readable_tool_errors(tool: str, arguments: dict) -> None:
    message = _call(tool, arguments)
    assert "未預期的錯誤（ValueError）" in message
    assert "Expecting value" not in message  # raw exception text is not leaked
