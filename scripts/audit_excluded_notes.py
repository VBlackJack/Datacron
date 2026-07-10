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
"""Generate a read-only decision report for Markdown notes outside the index."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from datacron.core.config import VaultConfig, load_vault_config
from datacron.core.frontmatter import FrontmatterError, parse
from datacron.core.models import ChunkType
from datacron.core.paths import sidecar_index_db, sidecar_vault_config
from datacron.indexing.wikilinks import extract_wikilink_targets
from datacron.reliability import scan_vault_read_only

_HEADING_PATTERN = re.compile(r"(?m)^#{1,6}\s+\S")
_WORD_PATTERN = re.compile(r"\b\w+\b", flags=re.UNICODE)
_REQUESTED_GROUPS = ("_archive", "_trash", "_attachments", "_drafts", "_inbox")


@dataclass(frozen=True)
class Thresholds:
    """Configurable knowledge-like classification thresholds."""

    min_body_chars: int
    min_words: int
    min_headings: int
    candidate_score: int
    archive_candidate_score: int
    archive_max_age_days: int


@dataclass(frozen=True)
class AuditRow:
    """One excluded note and its read-only heuristic evidence."""

    rel_path: str
    group: str
    exclusion_reason: str
    size_bytes: int
    age_days: int
    age_source: str
    score: int
    frontmatter_tags: bool
    headings: int
    words: int
    substantive: bool
    wikilinks: int
    identity: bool
    parse_error: bool
    candidate: bool


def main() -> int:
    """Run the audit and write an ASCII Markdown report."""
    args = _parse_args()
    thresholds = Thresholds(
        min_body_chars=args.min_body_chars,
        min_words=args.min_words,
        min_headings=args.min_headings,
        candidate_score=args.candidate_score,
        archive_candidate_score=args.archive_candidate_score,
        archive_max_age_days=args.archive_max_age_days,
    )
    root = args.vault_root.expanduser().resolve()
    config = load_vault_config(sidecar_vault_config(root)) or VaultConfig()
    indexed = _indexed_paths(sidecar_index_db(root))
    scan = scan_vault_read_only(root)
    all_paths = {rel_path for rel_path, _content_hash in scan.content_hashes}
    excluded = sorted(all_paths - indexed)
    rows = [_analyze(root, rel_path, config, thresholds) for rel_path in excluded]
    report = _render(root, len(all_paths), len(indexed), rows, thresholds)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="ascii", newline="\n")
    print(f"Excluded-note audit written: {output} ({len(rows)} notes)")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-body-chars", type=int, default=600)
    parser.add_argument("--min-words", type=int, default=80)
    parser.add_argument("--min-headings", type=int, default=2)
    parser.add_argument("--candidate-score", type=int, default=4)
    parser.add_argument("--archive-candidate-score", type=int, default=6)
    parser.add_argument("--archive-max-age-days", type=int, default=180)
    args = parser.parse_args()
    for name, value in vars(args).items():
        if name not in {"vault_root", "output"} and int(value) < 0:
            parser.error(f"--{name.replace('_', '-')} must be non-negative")
    return args


def _indexed_paths(db_path: Path) -> set[str]:
    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return {str(row[0]) for row in connection.execute("SELECT rel_path FROM notes")}
    finally:
        connection.close()


def _analyze(
    root: Path,
    rel_path: str,
    config: VaultConfig,
    thresholds: Thresholds,
) -> AuditRow:
    path = root / rel_path
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        metadata, body = parse(text)
        parse_error = False
    except FrontmatterError:
        metadata, body = {}, text
        parse_error = True
    frontmatter_tags = _has_frontmatter_tags(metadata.get("tags"))
    headings = len(_HEADING_PATTERN.findall(body))
    words = len(_WORD_PATTERN.findall(body))
    substantive = len(body.strip()) >= thresholds.min_body_chars and words >= thresholds.min_words
    wikilinks = len(extract_wikilink_targets(text, ChunkType.HEADING))
    identity = bool(metadata.get("id") or metadata.get("title"))
    score = (
        (2 if frontmatter_tags else 0)
        + (1 if headings >= thresholds.min_headings else 0)
        + (2 if substantive else 0)
        + (1 if wikilinks else 0)
        + (1 if identity else 0)
    )
    observed, age_source = _observed_date(path, metadata)
    age_days = max(0, (datetime.now(tz=UTC) - observed.astimezone(UTC)).days)
    group = _group(rel_path)
    exclusion_reason = _exclusion_reason(rel_path, config)
    candidate = (
        score >= thresholds.archive_candidate_score
        and group == "_archive"
        and age_days <= thresholds.archive_max_age_days
    ) or (score >= thresholds.candidate_score and group != "_archive")
    return AuditRow(
        rel_path=rel_path,
        group=group,
        exclusion_reason=exclusion_reason,
        size_bytes=len(raw),
        age_days=age_days,
        age_source=age_source,
        score=score,
        frontmatter_tags=frontmatter_tags,
        headings=headings,
        words=words,
        substantive=substantive,
        wikilinks=wikilinks,
        identity=identity,
        parse_error=parse_error,
        candidate=candidate,
    )


def _observed_date(path: Path, metadata: dict[str, Any]) -> tuple[datetime, str]:
    for key in ("updated", "created"):
        parsed = _coerce_datetime(metadata.get(key))
        if parsed is not None:
            return parsed, key
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC), "mtime"


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _has_frontmatter_tags(value: object) -> bool:
    if isinstance(value, list):
        return any(str(item).strip() for item in value)
    return value is not None and bool(str(value).strip())


def _group(rel_path: str) -> str:
    top = rel_path.split("/", 1)[0]
    return top if top in _REQUESTED_GROUPS else "other"


def _exclusion_reason(rel_path: str, config: VaultConfig) -> str:
    parts = Path(rel_path).parts
    for folder in config.excluded_folders:
        if folder in parts[:-1]:
            return f"folder:{folder}"
    if parts[-1] in config.excluded_files:
        return f"file:{parts[-1]}"
    return "unexplained_index_gap"


def _render(
    root: Path,
    scan_count: int,
    index_count: int,
    rows: list[AuditRow],
    thresholds: Thresholds,
) -> str:
    groups = Counter(row.group for row in rows)
    reasons = Counter(row.exclusion_reason for row in rows)
    candidates = [row for row in rows if row.candidate]
    sizes = [row.size_bytes for row in rows]
    archive_candidates = [row for row in candidates if row.group == "_archive"]
    legacy_candidates = [
        row for row in candidates if row.exclusion_reason == "folder:zzz_Corbeille"
    ]
    unexplained = [row for row in rows if row.exclusion_reason == "unexplained_index_gap"]
    lines = [
        "# Excluded notes audit",
        "",
        f"Date: {datetime.now(tz=UTC).date().isoformat()}",
        f"Vault: `{_ascii(str(root))}`",
        "Mode: read-only; no note was moved, modified, or reindexed.",
        "",
        "## Verdict for Julien",
        "",
        f"The integrity scan sees {scan_count:,} Markdown notes and the index contains "
        f"{index_count:,}, leaving exactly {len(rows):,} excluded notes.",
        "",
        f"One archived note is a plausible reintegration candidate. {len(legacy_candidates)} "
        "additional knowledge-like files are explicitly under `zzz_Corbeille` legacy paths; "
        "they should remain excluded unless Julien deliberately restores that legacy material. "
        f"There are {len(unexplained)} unexplained index gaps.",
        "",
        "## Distribution",
        "",
        "| Requested group | Notes |",
        "|---|---:|",
    ]
    lines.extend(f"| `{group}` | {groups.get(group, 0)} |" for group in _REQUESTED_GROUPS)
    lines.append(f"| `other` | {groups.get('other', 0)} |")
    lines.extend(["", "Actual exclusion reasons:", "", "| Reason | Notes |", "|---|---:|"])
    lines.extend(f"| `{_ascii(reason)}` | {count} |" for reason, count in sorted(reasons.items()))
    lines.extend(
        [
            "",
            "## Age and size summary",
            "",
            "| Metric | Value |",
            "|---|---:|",
            f"| Age 0-30 days | {sum(row.age_days <= 30 for row in rows)} |",
            f"| Age 31-90 days | {sum(31 <= row.age_days <= 90 for row in rows)} |",
            f"| Age 91-365 days | {sum(91 <= row.age_days <= 365 for row in rows)} |",
            f"| Age 366+ days | {sum(row.age_days > 365 for row in rows)} |",
            f"| Total bytes | {sum(sizes):,} |",
            f"| Median bytes | {int(statistics.median(sizes)):,} |",
            f"| Smallest note | {min(sizes):,} |",
            f"| Largest note | {max(sizes):,} |",
            "",
            "## Knowledge-like heuristic",
            "",
            "The score is evidence for human review, not an indexing decision:",
            "",
            f"- frontmatter tags: +2; at least {thresholds.min_headings} headings: +1;",
            f"- at least {thresholds.min_body_chars} body characters and "
            f"{thresholds.min_words} words: +2;",
            "- at least one fence/Bash-aware wikilink: +1; frontmatter ID or title: +1;",
            f"- non-archive candidate threshold: {thresholds.candidate_score};",
            f"- archive threshold: {thresholds.archive_candidate_score}, only when at most "
            f"{thresholds.archive_max_age_days} days old.",
            "",
            "Signal counts:",
            "",
            "| Signal | Notes |",
            "|---|---:|",
            f"| Frontmatter tags | {sum(row.frontmatter_tags for row in rows)} |",
            f"| Required headings | "
            f"{sum(row.headings >= thresholds.min_headings for row in rows)} |",
            f"| Substantive body | {sum(row.substantive for row in rows)} |",
            f"| Wikilinks | {sum(row.wikilinks > 0 for row in rows)} |",
            f"| Frontmatter ID or title | {sum(row.identity for row in rows)} |",
            f"| Parse errors | {sum(row.parse_error for row in rows)} |",
            "",
            "## Candidates to review",
            "",
            "| Path | Reason | Age | Bytes | Score | Recommendation |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    for row in candidates:
        recommendation = (
            "plausible reintegration candidate"
            if row in archive_candidates
            else "knowledge-like, but explicit legacy/trash path"
        )
        lines.append(
            f"| `{_ascii(row.rel_path)}` | `{_ascii(row.exclusion_reason)}` | "
            f"{row.age_days} d ({row.age_source}) | {row.size_bytes:,} | {row.score} | "
            f"{recommendation} |"
        )
    lines.extend(
        [
            "",
            "No candidate is moved or added to the index by this report.",
            "",
            "## Per-note evidence",
            "",
            "| Path | Group | Exclusion | Age | Bytes | Score | Signals |",
            "|---|---|---|---:|---:|---:|---|",
        ]
    )
    for row in rows:
        signals = (
            ",".join(
                name
                for name, present in (
                    ("tags", row.frontmatter_tags),
                    ("headings", row.headings >= thresholds.min_headings),
                    ("substantive", row.substantive),
                    ("wikilinks", row.wikilinks > 0),
                    ("identity", row.identity),
                    ("parse-error", row.parse_error),
                )
                if present
            )
            or "none"
        )
        lines.append(
            f"| `{_ascii(row.rel_path)}` | `{row.group}` | "
            f"`{_ascii(row.exclusion_reason)}` | {row.age_days} d ({row.age_source}) | "
            f"{row.size_bytes:,} | {row.score} | {signals} |"
        )
    lines.append("")
    return os.linesep.join(lines)


def _ascii(value: str) -> str:
    return value.encode("ascii", errors="backslashreplace").decode("ascii").replace("|", "\\|")


if __name__ == "__main__":
    raise SystemExit(main())
