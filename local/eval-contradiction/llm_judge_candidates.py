# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Julien Bombled
"""Replayable LLM judge over the frozen claim-decomposition ranking."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from claim_decomp_candidates import Note, load_corpus

LOGGER = logging.getLogger("datacron.contradiction.llm_judge")
PROVIDER_NAME = "llm-judge-frozen-ranking-v1"
DEFAULT_CONFIG: dict[str, Any] = {"provider": PROVIDER_NAME}

_TRIAL_METADATA: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()
_FEW_SHOT_PATTERN = re.compile(
    r"<!-- FEW_SHOT_JSON_START -->\s*(.*?)\s*<!-- FEW_SHOT_JSON_END -->",
    re.DOTALL,
)
_CONTEXT_STOPWORDS = {
    "avec",
    "dans",
    "des",
    "elle",
    "est",
    "for",
    "from",
    "les",
    "mais",
    "note",
    "pour",
    "that",
    "the",
    "this",
    "une",
    "with",
}
_NUMBER_WORDS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "un": "1",
    "deux": "2",
    "trois": "3",
    "quatre": "4",
    "cinq": "5",
    "sept": "7",
    "huit": "8",
    "neuf": "9",
    "dix": "10",
}
_JUDGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "contradiction": {"type": "boolean"},
        "polarity_conflict": {"type": "boolean"},
        "scope": {
            "type": "string",
            "enum": [
                "obligation",
                "activation",
                "replacement_state",
                "value_threshold",
                "different_scope",
                "insufficient_context",
                "other",
            ],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
    },
    "required": [
        "contradiction",
        "polarity_conflict",
        "scope",
        "confidence",
        "rationale",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class JudgeCandidate:
    """One frozen pair retained by the cached LLM judgment."""

    a: Note
    b: Note
    score: float
    frozen_rank: int
    frozen_score: float
    shared_subjects: list[str]
    conflict_types: list[str]
    claim_matches: list[dict[str, Any]]
    reasons: list[str]
    cache_key: str


def sha256_bytes(raw: bytes) -> str:
    """Return a lowercase SHA-256 digest."""
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a fixed input."""
    return sha256_bytes(path.read_bytes())


def canonical_json(value: Any) -> bytes:
    """Serialize a value deterministically for hashing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def frozen_ranking_hash(ranking: list[dict[str, Any]]) -> str:
    """Hash normalized frozen pairs while preserving their ranking order."""
    pairs = [[str(item["a"]), str(item["b"])] for item in ranking]
    return sha256_bytes(
        json.dumps(pairs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def load_frozen_evidence(config: dict[str, Any]) -> dict[str, Any]:
    """Load and fail closed when either frozen-input hash diverges."""
    path = Path(str(config["frozen_evidence_path"]))
    observed_file_hash = sha256_file(path)
    expected_file_hash = str(config["frozen_evidence_sha256"])
    if observed_file_hash != expected_file_hash:
        raise RuntimeError(
            f"Frozen evidence hash mismatch: {observed_file_hash} != {expected_file_hash}"
        )
    evidence = cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
    ranking = cast("list[dict[str, Any]]", evidence["ranking"])
    observed_ranking_hash = frozen_ranking_hash(ranking)
    expected_ranking_hash = str(config["frozen_ranking_sha256"])
    if observed_ranking_hash != expected_ranking_hash:
        raise RuntimeError(
            f"Frozen ranking hash mismatch: {observed_ranking_hash} != {expected_ranking_hash}"
        )
    expected_pairs = int(config["expected_pool_pairs"])
    if len(ranking) != expected_pairs:
        raise RuntimeError(f"Frozen ranking has {len(ranking)} pairs, expected {expected_pairs}")
    return evidence


def prompt_sha256(config: dict[str, Any]) -> str:
    """Return the digest of the versioned prompt bytes."""
    return sha256_file(Path(str(config["prompt_path"])))


def extract_few_shots(prompt_text: str) -> list[dict[str, Any]]:
    """Parse the machine-auditable few-shot block from the prompt."""
    match = _FEW_SHOT_PATTERN.search(prompt_text)
    if match is None:
        raise RuntimeError("Prompt is missing the auditable few-shot JSON block")
    return cast("list[dict[str, Any]]", json.loads(match.group(1)))


def _subject_terms(shared_subjects: list[str]) -> list[str]:
    terms = {
        cleaned
        for subject in shared_subjects
        if (cleaned := re.sub(r"[^a-z0-9_.+\\-]", "", subject.casefold()).strip("."))
    }
    return sorted(terms, key=lambda value: (-len(value), value))


def _context_terms(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9_.+\\-]+", text.casefold())
    return {
        _NUMBER_WORDS.get(token, token)
        for token in tokens
        if (len(token) >= 3 or token.isdigit()) and token not in _CONTEXT_STOPWORDS
    }


def note_context(
    note: Note,
    shared_subjects: list[str],
    counterpart_text: str,
    config: dict[str, Any],
) -> str:
    """Select deterministic high-signal note blocks around frozen shared subjects."""
    limit = int(config["max_note_context_chars"])
    maximum_blocks = int(config["max_context_blocks"])
    terms = _subject_terms(shared_subjects)
    counterpart_terms = _context_terms(counterpart_text)
    blocks = [
        block.strip() for block in re.split(r"\n\s*\n|^#{1,6}\s+", note.body, flags=re.MULTILINE)
    ]
    blocks = [block for block in blocks if block]
    scored: list[tuple[int, int, str]] = []
    for index, block in enumerate(blocks):
        lowered = block.casefold()
        shared_score = sum(term in lowered for term in terms)
        cross_note_score = len(_context_terms(block) & counterpart_terms)
        score = 2 * shared_score + 3 * cross_note_score
        if score:
            scored.append((score, index, block))
    ranked_blocks = sorted(scored, key=lambda item: (-item[0], item[1]))[:maximum_blocks]
    selected = [block for _, _, block in ranked_blocks]
    if not selected:
        selected = blocks[:maximum_blocks]
    header = f"Title: {note.title}\nTags: {', '.join(note.tags) or '(none)'}\n"
    return (header + "\n\n".join(selected))[:limit]


def pair_request(
    ranking_item: dict[str, Any],
    notes_by_path: dict[str, Note],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Build the exact content-addressed request for one frozen pair."""
    path_a = str(ranking_item["a"])
    path_b = str(ranking_item["b"])
    note_a = notes_by_path[path_a]
    note_b = notes_by_path[path_b]
    shared = [str(term) for term in ranking_item.get("shared_subjects", [])]
    full_a = f"{note_a.title}\n{note_a.body}"
    full_b = f"{note_b.title}\n{note_b.body}"
    context_a = note_context(note_a, shared, full_b, config)
    context_b = note_context(note_b, shared, full_a, config)
    normalized_pair = {
        "note_a": {"path": path_a, "context": context_a},
        "note_b": {"path": path_b, "context": context_b},
        "shared_subjects": shared,
    }
    fingerprint = {
        "model_id": str(config["model_id"]),
        "pair": normalized_pair,
        "prompt_sha256": prompt_sha256(config),
        "context_selection": str(config["context_selection"]),
    }
    cache_key = sha256_bytes(canonical_json(fingerprint))
    user_input = (
        "NOTE A\n"
        f"{context_a}\n\n"
        "NOTE B\n"
        f"{context_b}\n\n"
        f"Frozen shared-subject hints: {json.dumps(shared, ensure_ascii=False)}"
    )
    return {
        "cache_key": cache_key,
        "pair": [path_a, path_b],
        "context_sha256": sha256_bytes(canonical_json(normalized_pair)),
        "user_input": user_input,
    }


def _empty_cache(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "provider": PROVIDER_NAME,
        "provider_version": str(config["provider_version"]),
        "api_backend": str(config["api_backend"]),
        "model_id": str(config["model_id"]),
        "model_version": str(config["model_version"]),
        "prompt_sha256": prompt_sha256(config),
        "frozen_evidence_sha256": str(config["frozen_evidence_sha256"]),
        "frozen_ranking_sha256": str(config["frozen_ranking_sha256"]),
        "generated_at": None,
        "entries": {},
    }


def load_cache(config: dict[str, Any]) -> dict[str, Any]:
    """Load the content-addressed cache and validate its immutable namespace."""
    path = Path(str(config["cache_path"]))
    cache = (
        cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
        if path.exists()
        else _empty_cache(config)
    )
    expected = _empty_cache(config)
    for field in (
        "schema_version",
        "provider",
        "provider_version",
        "api_backend",
        "model_id",
        "model_version",
        "prompt_sha256",
        "frozen_evidence_sha256",
        "frozen_ranking_sha256",
    ):
        if cache.get(field) != expected[field]:
            raise RuntimeError(f"Cache namespace mismatch for {field}")
    return cache


def _write_cache(cache: dict[str, Any], config: dict[str, Any]) -> None:
    path = Path(str(config["cache_path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(cache, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _response_output_text(response: dict[str, Any]) -> str:
    for item in cast("list[dict[str, Any]]", response.get("output", [])):
        if item.get("type") != "message":
            continue
        for content in cast("list[dict[str, Any]]", item.get("content", [])):
            if content.get("type") == "output_text":
                return str(content["text"])
            if content.get("type") == "refusal":
                raise RuntimeError(
                    "The judge refused a benign contradiction-classification request"
                )
    raise RuntimeError("Responses API returned no structured output text")


def verify_backend_model(config: dict[str, Any]) -> None:
    """Fail closed if the configured local model tag moved to another digest."""
    if config["api_backend"] != "ollama_chat":
        return
    request = urllib.request.Request(  # noqa: S310 - loopback endpoint is explicit config
        str(config["model_registry_endpoint"]), method="GET"
    )
    with urllib.request.urlopen(  # noqa: S310 - loopback endpoint is explicit config
        request, timeout=float(config["request_timeout_seconds"])
    ) as raw_response:
        payload = cast("dict[str, Any]", json.loads(raw_response.read().decode("utf-8")))
    matches = [
        item
        for item in cast("list[dict[str, Any]]", payload.get("models", []))
        if item.get("name") == config["model_id"]
    ]
    if len(matches) != 1 or matches[0].get("digest") != config["model_version"]:
        raise RuntimeError("Configured local model digest is absent or has changed")


def _call_api(
    request_item: dict[str, Any],
    prompt_text: str,
    config: dict[str, Any],
    api_key: str | None,
) -> dict[str, Any]:
    if config["api_backend"] == "ollama_chat":
        payload: dict[str, Any] = {
            "model": str(config["model_id"]),
            "messages": [
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": str(request_item["user_input"])},
            ],
            "think": bool(config["think"]),
            "format": _JUDGMENT_SCHEMA,
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "temperature": float(config["temperature"]),
                "seed": int(config["seed"]),
                "num_predict": int(config["max_output_tokens"]),
            },
        }
    else:
        payload = {
            "model": str(config["model_id"]),
            "input": [
                {"role": "developer", "content": prompt_text},
                {"role": "user", "content": str(request_item["user_input"])},
            ],
            "temperature": float(config["temperature"]),
            "max_output_tokens": int(config["max_output_tokens"]),
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "contradiction_judgment",
                    "strict": True,
                    "schema": _JUDGMENT_SCHEMA,
                }
            },
        }
    body = canonical_json(payload)
    maximum_attempts = int(config["max_retries"]) + 1
    for attempt in range(1, maximum_attempts + 1):
        headers = {"Content-Type": "application/json"}
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(  # noqa: S310 - endpoint is explicit config
            str(config["api_endpoint"]),
            data=body,
            headers=headers,
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=float(config["request_timeout_seconds"])
            ) as raw_response:
                response = cast("dict[str, Any]", json.loads(raw_response.read().decode("utf-8")))
            latency = time.perf_counter() - started
            if config["api_backend"] == "ollama_chat":
                message = cast("dict[str, Any]", response["message"])
                judgment = cast("dict[str, Any]", json.loads(str(message["content"])))
                input_tokens = int(response.get("prompt_eval_count", 0))
                output_tokens = int(response.get("eval_count", 0))
                total_tokens = input_tokens + output_tokens
                response_id = ""
            else:
                judgment = cast("dict[str, Any]", json.loads(_response_output_text(response)))
                usage = cast("dict[str, Any]", response.get("usage", {}))
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
                total_tokens = int(usage.get("total_tokens", 0))
                response_id = str(response.get("id", ""))
            return {
                "judgment": judgment,
                "model": str(response.get("model", config["model_id"])),
                "response_id": response_id,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                },
                "latency_seconds": round(latency, 3),
                "attempts": attempt,
            }
        except urllib.error.HTTPError as exc:
            try:
                error_payload = cast("dict[str, Any]", json.loads(exc.read().decode("utf-8")))
                error_message = str(error_payload.get("error", {}).get("message", ""))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_message = ""
            retryable = exc.code in {408, 409, 429, 500, 502, 503, 504}
            if not retryable or attempt == maximum_attempts:
                detail = f": {error_message}" if error_message else ""
                raise RuntimeError(f"Responses API HTTP {exc.code}{detail}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt == maximum_attempts:
                raise RuntimeError("Responses API request exhausted retries") from exc
        time.sleep(float(config["retry_base_seconds"]) * (2 ** (attempt - 1)))
    raise AssertionError("unreachable")


def populate_cache(notes: list[Note], config: dict[str, Any]) -> dict[str, Any]:
    """Call the judge once for every missing frozen pair and persist each result."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if config["api_backend"] == "openai_responses" and not api_key:
        raise RuntimeError("OPENAI_API_KEY is required only to populate missing cache entries")
    verify_backend_model(config)
    evidence = load_frozen_evidence(config)
    ranking = cast("list[dict[str, Any]]", evidence["ranking"])
    notes_by_path = {note.path: note for note in notes}
    prompt_text = Path(str(config["prompt_path"])).read_text(encoding="utf-8")
    cache = load_cache(config)
    entries = cast("dict[str, dict[str, Any]]", cache["entries"])
    selected_ranking = ranking[: int(config["top_n"])]
    requests = [pair_request(item, notes_by_path, config) for item in selected_ranking]
    missing = [item for item in requests if item["cache_key"] not in entries]
    LOGGER.info("LLM judge cache: %d hits, %d missing", len(requests) - len(missing), len(missing))
    if not missing:
        return cache

    ranking_by_pair = {tuple(map(str, (item["a"], item["b"]))): item for item in selected_ranking}
    with ThreadPoolExecutor(max_workers=int(config["max_concurrency"])) as executor:
        futures = {
            executor.submit(_call_api, item, prompt_text, config, api_key): item for item in missing
        }
        for completed, future in enumerate(as_completed(futures), 1):
            item = futures[future]
            result = future.result()
            pair = cast("list[str]", item["pair"])
            frozen_item = ranking_by_pair[(pair[0], pair[1])]
            entry = {
                "cache_key": str(item["cache_key"]),
                "pair": pair,
                "frozen_rank": int(frozen_item["rank"]),
                "context_sha256": str(item["context_sha256"]),
                **result,
            }
            with _CACHE_LOCK:
                entries[str(item["cache_key"])] = entry
                cache["generated_at"] = datetime.now(UTC).isoformat()
                _write_cache(cache, config)
            if completed % 25 == 0 or completed == len(missing):
                LOGGER.info("Cached %d/%d missing judgments", completed, len(missing))
    return cache


def begin_trial(trial: int) -> None:
    """Reset replay metadata for one deterministic cache trial."""
    _TRIAL_METADATA.clear()
    _TRIAL_METADATA["trial"] = trial


def trial_metadata() -> dict[str, Any]:
    """Return metadata for the latest cache-only replay."""
    return dict(_TRIAL_METADATA)


def score_pairs(
    notes: list[Note], config: dict[str, Any], *, include_supersedes: bool = False
) -> list[JudgeCandidate]:
    """Filter the frozen ranking using cache only; network is impossible in this path."""
    del include_supersedes
    evidence = load_frozen_evidence(config)
    ranking = cast("list[dict[str, Any]]", evidence["ranking"])
    notes_by_path = {note.path: note for note in notes}
    cache = load_cache(config)
    entries = cast("dict[str, dict[str, Any]]", cache["entries"])
    threshold = float(config["confidence_threshold"])
    candidates: list[JudgeCandidate] = []
    cache_keys: list[str] = []
    for item in ranking[: int(config["top_n"])]:
        request_item = pair_request(item, notes_by_path, config)
        cache_key = str(request_item["cache_key"])
        cache_keys.append(cache_key)
        if cache_key not in entries:
            raise RuntimeError(
                f"Replay cache miss at frozen rank {item['rank']}; network is disabled"
            )
        cached = entries[cache_key]
        judgment = cast("dict[str, Any]", cached["judgment"])
        if not bool(judgment["contradiction"]) or float(judgment["confidence"]) < threshold:
            continue
        note_a = notes_by_path[str(item["a"])]
        note_b = notes_by_path[str(item["b"])]
        candidates.append(
            JudgeCandidate(
                a=note_a,
                b=note_b,
                score=float(judgment["confidence"]),
                frozen_rank=int(item["rank"]),
                frozen_score=float(item["score"]),
                shared_subjects=[str(term) for term in item.get("shared_subjects", [])],
                conflict_types=[str(judgment["scope"])],
                claim_matches=[
                    {
                        "judgment": judgment,
                        "frozen_rank": int(item["rank"]),
                        "frozen_score": float(item["score"]),
                    }
                ],
                reasons=[
                    "cached_llm_judgment",
                    f"confidence={float(judgment['confidence']):.6f}",
                    f"scope={judgment['scope']}",
                ],
                cache_key=cache_key,
            )
        )
    payload = [candidate_payload(candidate) for candidate in candidates]
    _TRIAL_METADATA.update(
        {
            "cache_only": True,
            "cache_hits": len(cache_keys),
            "cache_misses": 0,
            "cache_sha256": sha256_file(Path(str(config["cache_path"]))),
            "prompt_sha256": prompt_sha256(config),
            "frozen_ranking_sha256": frozen_ranking_hash(ranking),
            "ranking_sha256": sha256_bytes(canonical_json(payload)),
            "retained_pairs": len(candidates),
        }
    )
    return candidates


def candidate_payload(candidate: JudgeCandidate) -> dict[str, Any]:
    """Return stable replay evidence for one retained pair."""
    return {
        "a": candidate.a.path,
        "b": candidate.b.path,
        "cache_key": candidate.cache_key,
        "confidence": round(candidate.score, 6),
        "frozen_rank": candidate.frozen_rank,
        "frozen_score": round(candidate.frozen_score, 6),
        "judgment": candidate.claim_matches[0]["judgment"],
        "reasons": candidate.reasons,
    }


def signal_contributions(candidate: JudgeCandidate, config: dict[str, Any]) -> dict[str, float]:
    """Expose judge confidence through the generic provider interface."""
    del config
    return {"llm_confidence": candidate.score}


def main() -> int:
    """Populate missing cache entries for the frozen ranking."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--populate-cache", action="store_true", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = cast("dict[str, Any]", json.loads(args.config.read_text(encoding="utf-8")))
    notes = load_corpus(Path(str(config["corpus_path"])), config)
    cache = populate_cache(notes, config)
    LOGGER.info(
        "Cache complete: %d/%d entries",
        len(cast("dict[str, Any]", cache["entries"])),
        int(config["expected_pool_pairs"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
