# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Measure targeted writes and global reconciliation on disposable synthetic vaults."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import tempfile
from pathlib import Path
from time import perf_counter

from datacron import __version__
from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.paths import sidecar_index_db
from datacron.indexing.reconcile import reconcile
from datacron.mcp.server import build_app
from datacron.mcp.tools.write import _append_journal_impl

_DEFAULT_SIZES = (100, 1000, 5000)
_DEFAULT_REPEATS = 5
_MILLISECONDS = 1000


async def measure(size: int, repeats: int) -> dict[str, object]:
    """Measure warm operations after an initial complete index, without production data."""
    with tempfile.TemporaryDirectory(prefix="datacron-benchmark-") as directory:
        root = Path(directory)
        for index in range(size):
            (root / f"note-{index}.md").write_text(
                serialize({"title": f"Note {index}"}, "# Note\n\n## Journal\n\nBody\n"),
                encoding="utf-8",
            )
        app = build_app(
            settings=Settings(vault_root=root, read_paths=[root], write_paths=[root]),
            vault_root=root,
        )
        await app.store.open(sidecar_index_db(root))
        writes = []
        scans = []
        try:
            await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
            for index in range(repeats):
                started = perf_counter()
                result = await _append_journal_impl(
                    app, rel_path="note-0.md", heading="Journal", entry=f"Measurement {index}"
                )
                writes.append((perf_counter() - started) * _MILLISECONDS)
                if "error" in result:
                    raise RuntimeError(result["error"])
                started = perf_counter()
                await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
                scans.append((perf_counter() - started) * _MILLISECONDS)
        finally:
            await app.store.close()
        return {
            "notes": size,
            "repeats": repeats,
            "targeted_write_ms": writes,
            "global_reconcile_ms": scans,
            "targeted_write_median_ms": statistics.median(writes),
            "global_reconcile_median_ms": statistics.median(scans),
        }


async def main() -> None:
    """Print measurements with version and host provenance as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=_DEFAULT_SIZES)
    parser.add_argument("--repeats", type=int, default=_DEFAULT_REPEATS)
    args = parser.parse_args()
    if args.repeats < 1 or any(size < 1 for size in args.sizes):
        parser.error("sizes and repeats must be positive")
    results = [await measure(size, args.repeats) for size in args.sizes]
    print(
        json.dumps(
            {
                "version": __version__,
                "platform": platform.platform(),
                "python": platform.python_version(),
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
