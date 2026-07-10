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
"""Refresh or enforce the read-only reliability baseline for a mounted vault."""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from datacron.core.config import Settings
from datacron.core.logger import configure_logging, get_logger, shutdown_logging
from datacron.reliability import (
    ReliabilityScan,
    compare_with_baseline,
    scan_vault_read_only,
)


def main() -> int:
    """Run a read-only scan, then refresh or enforce its accepted baseline."""
    args = _parse_args()
    output = args.output.expanduser().resolve()
    configure_logging(Settings(log_dir=output.parent / "logs"))
    logger = get_logger(__name__)
    try:
        scan = scan_vault_read_only(args.vault_root)
        if args.refresh:
            baseline = _full_baseline(args.vault_root, scan)
            _write_json(output, baseline)
            if args.policy_output is not None:
                _write_json(args.policy_output, _sanitized_policy(scan))
            print(_summary(scan, new_count=0, mode="refreshed"))
            logger.info("reliability baseline refreshed at %s", output)
            return 0

        policy = _load_policy(args.policy or output)
        comparison = compare_with_baseline(scan, policy)
        result: dict[str, object] = {
            "scan": scan.to_dict(),
            "comparison": {
                "passed": comparison.passed,
                "new_violations": [item.to_dict() for item in comparison.new_violations],
                "accepted_count": len(comparison.accepted_violations),
                "stale_fingerprints": list(comparison.stale_fingerprints),
            },
        }
        _write_json(output, result)
        print(
            _summary(
                scan,
                new_count=len(comparison.new_violations),
                mode="enforced",
            )
        )
        return 0 if comparison.passed and not scan.parse_errors else 1
    finally:
        shutdown_logging()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--policy-output", type=Path)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.policy_output is not None and not args.refresh:
        parser.error("--policy-output requires --refresh")
    return args


def _full_baseline(vault_root: Path, scan: ReliabilityScan) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "scan_mode": "read_only",
        "vault_root": str(vault_root.expanduser().resolve()),
        "scan": scan.to_dict(),
        "accepted_fingerprints": _fingerprints_by_kind(scan),
    }


def _sanitized_policy(scan: ReliabilityScan) -> dict[str, object]:
    grouped = _fingerprints_by_kind(scan)
    return {
        "schema_version": 1,
        "description": "SHA-256 keys for accepted private-vault reliability debt.",
        "accepted_fingerprints": grouped,
        "accepted_counts": {kind: len(values) for kind, values in grouped.items()},
    }


def _fingerprints_by_kind(scan: ReliabilityScan) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for violation in scan.violations:
        grouped[violation.kind].append(violation.fingerprint)
    return {kind: sorted(values) for kind, values in sorted(grouped.items())}


def _load_policy(path: Path) -> Mapping[str, Sequence[str]]:
    payload: Any = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(payload, dict):
        raise ValueError("baseline policy must be a JSON object")
    accepted = payload.get("accepted_fingerprints")
    if not isinstance(accepted, dict):
        raise ValueError("baseline policy lacks accepted_fingerprints")
    policy: dict[str, list[str]] = {}
    for kind, values in accepted.items():
        if not isinstance(kind, str) or not isinstance(values, list):
            raise ValueError("accepted_fingerprints must map strings to lists")
        if not all(isinstance(value, str) for value in values):
            raise ValueError("accepted fingerprints must be strings")
        policy[kind] = list(values)
    return policy


def _write_json(path: Path, payload: object) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + os.linesep,
        encoding="ascii",
        newline="",
    )


def _summary(scan: ReliabilityScan, *, new_count: int, mode: str) -> str:
    return (
        f"Reliability scan {mode}: notes={scan.notes_count} "
        f"id_violations={len(scan.id_violations)} "
        f"broken_wikilinks={len(scan.broken_wikilinks)} "
        f"supersedes_cycles={len(scan.supersedes_cycles)} "
        f"parse_errors={len(scan.parse_errors)} new={new_count}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
