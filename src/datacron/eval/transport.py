# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Optional end-to-end MCP transport used by the local eval smoke mode."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from datacron.core.config import Settings
from datacron.eval.harness import ToolSearch

__all__ = ["e2e_search_transport"]


@asynccontextmanager
async def e2e_search_transport(
    vault_root: Path,
    settings: Settings,
) -> AsyncIterator[ToolSearch]:
    """Yield a ``search_text`` callback backed by one initialized stdio session."""
    params = _server_parameters(vault_root, settings)
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()

        async def _search(query: str, limit: int) -> dict[str, Any]:
            response = await session.call_tool(
                "search_text",
                {"query": query, "limit": limit},
            )
            if response.isError:
                raise RuntimeError(f"MCP search_text failed: {_response_text(response.content)}")
            payload = json.loads(_response_text(response.content))
            if not isinstance(payload, dict):
                raise TypeError("MCP search_text returned a non-object JSON payload")
            return payload

        yield _search


def _server_parameters(vault_root: Path, settings: Settings) -> StdioServerParameters:
    """Launch the project server with the effective eval settings."""
    env = dict(os.environ)
    read_paths = settings.read_paths or [vault_root]
    env.update(
        {
            "DATACRON_VAULT_ROOT": str(vault_root),
            "DATACRON_READ_PATHS": os.pathsep.join(str(path) for path in read_paths),
            "DATACRON_WRITE_PATHS": os.pathsep.join(str(path) for path in settings.write_paths),
            "DATACRON_READ_ONLY": str(settings.read_only).lower(),
            "DATACRON_MAX_RESULT_COUNT": str(settings.max_result_count),
            "DATACRON_MAX_RESULT_TOKENS": str(settings.max_result_tokens),
            "DATACRON_LOG_DIR": str(settings.log_dir),
            "DATACRON_LOG_LEVEL": settings.log_level,
            "PYTHONUNBUFFERED": "1",
        }
    )
    return StdioServerParameters(
        command=sys.executable,
        args=["-c", "from datacron.cli import mcp_entry; mcp_entry()"],
        env=env,
    )


def _response_text(content: list[Any]) -> str:
    """Extract the JSON text block emitted by a FastMCP dictionary result."""
    for block in content:
        if isinstance(block, TextContent):
            return block.text
    raise TypeError("MCP search_text response contains no text block")
