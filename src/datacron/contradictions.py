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
"""Deterministic, cache-only contradiction advisory over a frozen pool."""

from __future__ import annotations

import gzip
import hashlib
import json
from importlib import resources
from typing import Any, Final, cast

__all__ = [
    "ADVISORY_WARNING",
    "FrozenContradictionError",
    "build_advisory_report",
    "unavailable_advisory_report",
]

ADVISORY_WARNING: Final[str] = (
    "ADVISORY ONLY — NOT VALIDATED ON REAL CONTENT (0/4). JUDGE CONFIDENCE IS "
    "UNCALIBRATED. DO NOT BLOCK WRITES, MERGES, HEALTH, OR CI BASED ON THIS REPORT."
)

_DATA_DIRECTORY: Final[str] = "contradiction_data"
_EVIDENCE_RESOURCE: Final[str] = "frozen-evidence.json.gz"
_CACHE_RESOURCE: Final[str] = "judge-cache.json.gz"
_ASSERTIONS_RESOURCE: Final[str] = "source-assertions.json.gz"
_EVIDENCE_SHA256: Final[str] = "eb421b383c460440cc4749ef649f3f7e23280b8d91d282d079b411f19c80012a"
_RANKING_SHA256: Final[str] = "f6e1c1d31a40da7134b285dce1f0690bd033680b22d1fcac8cbc2aa9c619681b"
_CACHE_SHA256: Final[str] = "4f67b276b55d01e36c82479032d938d73324f01c6fad7fbd5990243e79d5e0e5"
_ASSERTIONS_SHA256: Final[str] = "1421d79fc6c4abe0f54f99d0c2a0e230d114cb5bd527864fe98719e4cc8712c0"
_RETAINED_RANKING_SHA256: Final[str] = (
    "f1db36e3e428677e0daf185360d2e729daa4e12d31624e2fb5cc089f74f5cb7c"
)
_POOL_PAIRS: Final[int] = 393
_UNJUDGED_PAIRS: Final[int] = 254
_CONFIDENCE_THRESHOLD: Final[float] = 0.7


class FrozenContradictionError(RuntimeError):
    """Raised when a packaged advisory artifact violates its frozen contract."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_resource(name: str, expected_sha256: str) -> dict[str, Any]:
    try:
        compressed = resources.files("datacron").joinpath(_DATA_DIRECTORY, name).read_bytes()
        raw = gzip.decompress(compressed)
    except OSError as exc:
        raise FrozenContradictionError(f"frozen advisory resource unavailable: {name}") from exc
    observed = _sha256(raw)
    if observed != expected_sha256:
        raise FrozenContradictionError(
            f"frozen advisory resource hash mismatch for {name}: {observed}"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrozenContradictionError(f"frozen advisory resource is invalid: {name}") from exc
    if not isinstance(payload, dict):
        raise FrozenContradictionError(f"frozen advisory resource must be an object: {name}")
    return cast("dict[str, Any]", payload)


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FrozenContradictionError(f"{label} must be an object")
    return cast("dict[str, Any]", value)


def _sequence(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FrozenContradictionError(f"{label} must be an array")
    return value


def _pair(value: Any, *, label: str) -> tuple[str, str]:
    items = _sequence(value, label=label)
    if len(items) != 2 or any(not isinstance(item, str) for item in items):
        raise FrozenContradictionError(f"{label} must contain two paths")
    return str(items[0]), str(items[1])


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FrozenContradictionError(f"{label} must be an integer")
    return cast("int", value)


def _number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FrozenContradictionError(f"{label} must be numeric")
    return float(value)


def _ranked_pool(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    ranking_raw = _sequence(evidence.get("ranking"), label="frozen ranking")
    if len(ranking_raw) != _POOL_PAIRS:
        raise FrozenContradictionError("frozen ranking must contain exactly 393 pairs")
    ranking: list[dict[str, Any]] = []
    pairs: list[list[str]] = []
    for expected_rank, raw_item in enumerate(ranking_raw, 1):
        item = _mapping(raw_item, label=f"frozen rank {expected_rank}")
        rank = _integer(item.get("rank"), label="frozen rank")
        if rank != expected_rank:
            raise FrozenContradictionError("frozen ranking order is not canonical")
        pair = (str(item.get("a", "")), str(item.get("b", "")))
        if not all(pair):
            raise FrozenContradictionError(f"frozen rank {rank} has an invalid pair")
        pairs.append([pair[0], pair[1]])
        ranking.append(item)
    observed = _sha256(json.dumps(pairs, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if observed != _RANKING_SHA256:
        raise FrozenContradictionError(f"frozen ranking hash mismatch: {observed}")
    return ranking


def _cache_by_rank(cache: dict[str, Any]) -> dict[int, dict[str, Any]]:
    if cache.get("frozen_evidence_sha256") != _EVIDENCE_SHA256:
        raise FrozenContradictionError("cache evidence namespace mismatch")
    if cache.get("frozen_ranking_sha256") != _RANKING_SHA256:
        raise FrozenContradictionError("cache ranking namespace mismatch")
    entries = _mapping(cache.get("entries"), label="judge cache entries")
    if len(entries) != _POOL_PAIRS:
        raise FrozenContradictionError("judge cache must contain exactly 393 entries")
    by_rank: dict[int, dict[str, Any]] = {}
    for cache_key, raw_entry in entries.items():
        entry = _mapping(raw_entry, label="judge cache entry")
        if entry.get("cache_key") != cache_key or len(cache_key) != 64:
            raise FrozenContradictionError("judge cache key is not content addressed")
        context_sha256 = entry.get("context_sha256")
        if not isinstance(context_sha256, str) or len(context_sha256) != 64:
            raise FrozenContradictionError("judge cache context hash is invalid")
        rank = _integer(entry.get("frozen_rank"), label="cached frozen rank")
        if rank in by_rank:
            raise FrozenContradictionError(f"duplicate cached frozen rank: {rank}")
        _pair(entry.get("pair"), label=f"cached pair at rank {rank}")
        by_rank[rank] = entry
    return by_rank


def _retained_candidates(
    ranking: list[dict[str, Any]],
    cache_by_rank: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    for item in ranking:
        rank = _integer(item.get("rank"), label="frozen rank")
        pair = (str(item["a"]), str(item["b"]))
        cached = cache_by_rank.get(rank)
        if cached is None or _pair(cached.get("pair"), label="cached pair") != pair:
            raise FrozenContradictionError(f"judge cache misses frozen rank {rank}")
        judgment = _mapping(cached.get("judgment"), label="cached judgment")
        contradiction = judgment.get("contradiction")
        if not isinstance(contradiction, bool):
            raise FrozenContradictionError("cached contradiction decision must be boolean")
        confidence = _number(judgment.get("confidence"), label="judge confidence")
        if not contradiction or confidence < _CONFIDENCE_THRESHOLD:
            continue
        scope = judgment.get("scope")
        if not isinstance(scope, str):
            raise FrozenContradictionError("cached judgment scope must be text")
        retained.append(
            {
                "a": pair[0],
                "b": pair[1],
                "cache_key": str(cached["cache_key"]),
                "confidence": round(confidence, 6),
                "frozen_rank": rank,
                "frozen_score": round(
                    _number(item.get("score"), label="frozen score"),
                    6,
                ),
                "judgment": judgment,
                "reasons": [
                    "cached_llm_judgment",
                    f"confidence={confidence:.6f}",
                    f"scope={scope}",
                ],
            }
        )
    observed = _sha256(_canonical_json(retained))
    if observed != _RETAINED_RANKING_SHA256:
        raise FrozenContradictionError(f"retained ranking hash mismatch: {observed}")
    return retained


def _source_rows(assertions: dict[str, Any]) -> dict[int, dict[str, Any]]:
    expected_headers = {
        "frozen_evidence_sha256": _EVIDENCE_SHA256,
        "frozen_ranking_sha256": _RANKING_SHA256,
        "retained_ranking_sha256": _RETAINED_RANKING_SHA256,
    }
    if any(assertions.get(key) != value for key, value in expected_headers.items()):
        raise FrozenContradictionError("source assertion namespace mismatch")
    rows = _sequence(assertions.get("candidates"), label="source assertions")
    by_rank: dict[int, dict[str, Any]] = {}
    for raw_row in rows:
        row = _mapping(raw_row, label="source assertion row")
        rank = _integer(row.get("frozen_rank"), label="source assertion rank")
        if rank in by_rank:
            raise FrozenContradictionError(f"duplicate source assertion rank: {rank}")
        by_rank[rank] = row
    return by_rank


def _claim_payload(value: Any, *, label: str) -> dict[str, Any]:
    claim = _mapping(value, label=label)
    text = claim.get("text")
    if not isinstance(text, str) or not text.strip():
        raise FrozenContradictionError(f"{label} has no source assertion")
    return claim


def _enrich_candidates(
    retained: list[dict[str, Any]],
    sources_by_rank: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for output_rank, candidate in enumerate(retained, 1):
        frozen_rank = _integer(candidate.get("frozen_rank"), label="retained frozen rank")
        source = sources_by_rank.get(frozen_rank)
        if source is None:
            raise FrozenContradictionError(f"source assertions miss frozen rank {frozen_rank}")
        pair = (str(candidate["a"]), str(candidate["b"]))
        if _pair(source.get("pair"), label="source assertion pair") != pair:
            raise FrozenContradictionError(f"source assertion pair mismatch at rank {frozen_rank}")
        if source.get("cache_key") != candidate["cache_key"]:
            raise FrozenContradictionError(f"source assertion cache mismatch at rank {frozen_rank}")
        status = source.get("adjudication_status")
        if status not in {"adjudicated", "unjudged"}:
            raise FrozenContradictionError("invalid advisory adjudication status")
        judgment = _mapping(candidate["judgment"], label="retained judgment")
        enriched.append(
            {
                "advisory_rank": output_rank,
                "frozen_rank": frozen_rank,
                "confidence": candidate["confidence"],
                "confidence_calibrated": False,
                "scope": judgment["scope"],
                "rationale": judgment["rationale"],
                "adjudication_status": status,
                "cache_key": candidate["cache_key"],
                "left": {
                    "path": pair[0],
                    "source_assertion": _claim_payload(
                        source.get("assertion_a"),
                        label="left source assertion",
                    ),
                },
                "right": {
                    "path": pair[1],
                    "source_assertion": _claim_payload(
                        source.get("assertion_b"),
                        label="right source assertion",
                    ),
                },
            }
        )
    return enriched


def _effects() -> dict[str, str]:
    return {
        "writes": "none",
        "merges": "none",
        "health": "none",
        "ci": "none",
    }


def build_advisory_report() -> dict[str, Any]:
    """Replay the frozen generator ranking through cached judgments only."""
    evidence = _load_resource(_EVIDENCE_RESOURCE, _EVIDENCE_SHA256)
    cache = _load_resource(_CACHE_RESOURCE, _CACHE_SHA256)
    assertions = _load_resource(_ASSERTIONS_RESOURCE, _ASSERTIONS_SHA256)
    ranking = _ranked_pool(evidence)
    cache_by_rank = _cache_by_rank(cache)
    retained = _retained_candidates(ranking, cache_by_rank)
    candidates = _enrich_candidates(retained, _source_rows(assertions))
    retained_unjudged = sum(
        candidate["adjudication_status"] == "unjudged" for candidate in candidates
    )
    return {
        "warning": ADVISORY_WARNING,
        "advisory_only": True,
        "available": True,
        "effects": _effects(),
        "frozen_input": {
            "pool_pairs": len(ranking),
            "evidence_sha256": _EVIDENCE_SHA256,
            "ranking_sha256": _RANKING_SHA256,
        },
        "cache_replay": {
            "mode": "content_addressed_cache_only",
            "cache_sha256": _CACHE_SHA256,
            "cache_entries": len(cache_by_rank),
            "cache_hits": len(ranking),
            "cache_misses": 0,
            "network_calls": 0,
            "replay_sha256": _RETAINED_RANKING_SHA256,
        },
        "human_adjudication_backlog": {
            "unjudged_pairs": _UNJUDGED_PAIRS,
            "retained_candidates": retained_unjudged,
            "included_in_precision": False,
        },
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def unavailable_advisory_report() -> dict[str, Any]:
    """Return a non-blocking response when the frozen replay is unavailable."""
    return {
        "warning": ADVISORY_WARNING,
        "advisory_only": True,
        "available": False,
        "effects": _effects(),
        "candidate_count": 0,
        "candidates": [],
        "error": {
            "type": "FrozenReplayUnavailable",
            "message": "Frozen contradiction advisory replay is unavailable.",
        },
    }
