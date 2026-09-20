#!/usr/bin/env python3
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
"""Run a freshly built binary as an MCP server and complete one tool round trip.

The release workflow only ran the binary with ``--help``, which touches the CLI
parser and nothing else. A one-file bundle fails at import time, on the user's
machine, when a module reached only through a dynamic import was not collected -
and ``--help`` returns before any of that is imported. This starts the server the
way a client does, indexes a throwaway vault, and reads a note back, so a bundle
that cannot serve is caught before the release is published rather than after.

It is deliberately not the Windows installation verifier: no install, no
registry, no user profile. It answers one question about one executable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters, stdio_client
from mcp.client.session import ClientSession
from mcp.types import TextContent

_CANARY: str = "ReleaseSmokeCanary"
_TIMEOUT_SECONDS: int = 180


def _build_vault(root: Path) -> Path:
    """Write the smallest vault that proves retrieval works end to end."""
    vault = root / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "welcome.md").write_text(
        "# Welcome\n\n## Canary\n\n" + _CANARY + " must come back from a search.\n",
        encoding="utf-8",
    )
    return vault


async def _round_trip(executable: Path, vault: Path) -> dict[str, Any]:
    """Start the binary over stdio and call the three tools a client starts with."""
    params = StdioServerParameters(
        command=str(executable), args=["mcp", "serve", "--vault", str(vault)]
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        health = await session.call_tool("get_health", {})
        search = await session.call_tool("search_text", {"query": _CANARY})
        payloads = []
        for response in (health, search):
            if response.is_error or not isinstance(response.content[0], TextContent):
                raise RuntimeError("the released binary returned a tool error")
            payloads.append(json.loads(response.content[0].text))
        if not payloads[1]["returned"]:
            raise RuntimeError("the released binary could not retrieve the canary note")
        return {
            "tools": len(tools.tools),
            "server_version": payloads[0]["server_version"],
            "returned": payloads[1]["returned"],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path, help="the built datacron binary")
    arguments = parser.parse_args()
    executable = arguments.executable.expanduser().resolve()
    if not executable.is_file():
        print(f"No such executable: {executable}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as workspace:
        vault = _build_vault(Path(workspace))
        indexed = subprocess.run(  # noqa: S603 - the executable is a resolved argument
            [str(executable), "index", "--vault", str(vault)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
        if indexed.returncode != 0:
            print(indexed.stdout, file=sys.stderr)
            print(indexed.stderr, file=sys.stderr)
            print("the released binary could not index a fresh vault", file=sys.stderr)
            return 1
        try:
            evidence = asyncio.run(
                asyncio.wait_for(_round_trip(executable, vault), _TIMEOUT_SECONDS)
            )
        except (TimeoutError, RuntimeError, OSError) as exc:
            print(f"the released binary failed its MCP round trip: {exc}", file=sys.stderr)
            return 1

    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
