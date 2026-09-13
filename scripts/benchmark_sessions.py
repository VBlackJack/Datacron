# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Measure independent MCP processes sharing disposable synthetic vaults."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any

from datacron import __version__
from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.paths import sidecar_index_db
from datacron.eval.transport import e2e_tool_transport
from datacron.indexing.reconcile import reconcile
from datacron.mcp.server import build_app

_DEFAULT_SIZES = (100, 1000, 5000)
_DEFAULT_CLIENTS = 3
_DEFAULT_REPEATS = 10
_MILLISECONDS = 1000
_P95 = 0.95


def summary(samples: list[float]) -> dict[str, Any]:
    """Return raw samples and nearest-rank p95, never interpolate a tiny sample."""
    return {
        "samples_ms": samples,
        "median_ms": statistics.median(samples),
        "p95_ms": sorted(samples)[math.ceil(len(samples) * _P95) - 1],
    }


async def client(root: Path, config: Settings, ordinal: int, repeats: int) -> dict[str, Any]:
    started = perf_counter()
    searches: list[float] = []
    writes: list[float] = []
    errors: list[str] = []
    async with e2e_tool_transport(root, config) as call:
        startup = (perf_counter() - started) * _MILLISECONDS
        for iteration in range(repeats):
            for tool, arguments, samples in (
                ("search_text", {"query": f"Measurement{ordinal}", "limit": 5}, searches),
                (
                    "append_journal",
                    {
                        "rel_path": f"note-{ordinal}.md",
                        "heading": "Journal",
                        "entry": f"Measurement {iteration}",
                        "request_id": f"client-{ordinal}-{iteration}",
                    },
                    writes,
                ),
            ):
                before = perf_counter()
                result = await call(tool, arguments)
                samples.append((perf_counter() - before) * _MILLISECONDS)
                if "error" in result:
                    errors.append(str(result["error"].get("code", result["error"].get("type"))))
                elif tool == "append_journal" and result.get("indexed") is not True:
                    errors.append("missing_index_confirmation")
                elif tool == "search_text" and not result.get("returned"):
                    errors.append("missing_search_hit")
    return {
        "client": ordinal,
        "startup_ms": startup,
        "search": summary(searches),
        "write": summary(writes),
        "errors": errors,
    }


async def measure(size: int, clients: int, repeats: int) -> dict[str, Any]:
    """Index once, close the seed connection, then use concurrent OS processes."""
    with tempfile.TemporaryDirectory(prefix="datacron-sessions-") as directory:
        root = Path(directory)
        for number in range(size):
            (root / f"note-{number}.md").write_text(
                serialize(
                    {"title": f"Measurement{number}"},
                    f"# Measurement{number}\n\n## Journal\nBody.\n",
                ),
                encoding="utf-8",
            )
        config = Settings(
            vault_root=root, read_paths=[root], write_paths=[root], log_dir=root / "logs"
        )
        app = build_app(settings=config, vault_root=root)
        await app.store.open(sidecar_index_db(root))
        try:
            await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=False)
        finally:
            await app.store.close()
        measurements = await asyncio.gather(
            *(client(root, config, n, repeats) for n in range(clients))
        )
        return {"notes": size, "clients": measurements}


async def main() -> None:
    """Emit host/version evidence and fail the campaign on any observed tool error."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=_DEFAULT_SIZES)
    parser.add_argument("--clients", type=int, default=_DEFAULT_CLIENTS)
    parser.add_argument("--repeats", type=int, default=_DEFAULT_REPEATS)
    args = parser.parse_args()
    if min(args.clients, args.repeats, *args.sizes) < 1 or min(args.sizes) < args.clients:
        parser.error("positive sizes, clients and repeats required; sizes must cover every client")
    results = [await measure(size, args.clients, args.repeats) for size in args.sizes]
    failures = sum(len(c["errors"]) for r in results for c in r["clients"])
    print(
        json.dumps(
            {
                "version": __version__,
                "platform": platform.platform(),
                "python": platform.python_version(),
                "repeats": args.repeats,
                "failures": failures,
                "results": results,
            },
            indent=2,
        )
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
