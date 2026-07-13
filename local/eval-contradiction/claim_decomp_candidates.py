# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Julien Bombled
"""Deterministic claim-decomposition contradiction candidate provider."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, cast

LOGGER = logging.getLogger("datacron.contradiction.claim_decomp")
PROVIDER_NAME = "claim-decomposition-rules-v1"
DEFAULT_CONFIG: dict[str, Any] = {"provider": PROVIDER_NAME}


@dataclass(frozen=True)
class Note:
    """One immutable evaluation note."""

    note_id: str
    path: str
    title: str
    tags: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class Claim:
    """One normalized atomic claim extracted from a note segment."""

    claim_id: str
    subject: tuple[str, ...]
    scope: str
    polarity: str
    value_or_state: tuple[str, ...]
    temporal_context: tuple[str, ...]
    text: str


@dataclass
class Candidate:
    """A note pair containing at least one potentially conflicting claim pair."""

    a: Note
    b: Note
    score: float
    shared_subjects: list[str]
    conflict_types: list[str]
    claim_matches: list[dict[str, Any]]
    reasons: list[str]


_TRIAL_CACHE: dict[str, Any] = {}
_TRIAL_METADATA: dict[str, Any] = {}


def begin_trial(trial: int) -> None:
    """Reset all derived state so each trial exercises extraction and ranking."""
    _TRIAL_CACHE.clear()
    _TRIAL_METADATA.clear()
    _TRIAL_METADATA["trial"] = trial


def trial_metadata() -> dict[str, Any]:
    """Return deterministic extraction metadata for the current trial."""
    return dict(_TRIAL_METADATA)


def load_corpus(corpus_path: Path, config: dict[str, Any]) -> list[Note]:
    """Load the fixed derived corpus without touching the source vault."""
    del config
    data = cast("list[dict[str, Any]]", json.loads(corpus_path.read_text(encoding="utf-8")))
    notes = [
        Note(
            note_id=str(item["id"]),
            path=str(item["path"]),
            title=str(item["title"]),
            tags=tuple(str(tag) for tag in item.get("tags", [])),
            body=str(item.get("body", "")),
        )
        for item in data
    ]
    LOGGER.info("Loaded %d notes from corpus %s", len(notes), corpus_path)
    return notes


def _load_rules(config: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(config["rules_path"]))
    rules = cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
    _TRIAL_METADATA["rules_path"] = path.as_posix()
    _TRIAL_METADATA["rules_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return rules


def _ascii_lower(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


def _normalize(text: str, rules: dict[str, Any]) -> str:
    normalized = _ascii_lower(text)
    aliases = cast("dict[str, list[str]]", rules["aliases"])
    for canonical, variants in aliases.items():
        for variant in sorted(variants, key=len, reverse=True):
            pattern = rf"(?<!\w){re.escape(_ascii_lower(variant))}(?!\w)"
            normalized = re.sub(pattern, canonical, normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _tokens(text: str, rules: dict[str, Any]) -> list[str]:
    return re.findall(str(rules["token_pattern"]), text)


def _contains_term(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None


def _matching_terms(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if _contains_term(text, term)]


def _segments(note: Note, rules: dict[str, Any], config: dict[str, Any]) -> list[str]:
    raw_segments: list[str] = [note.title]
    for line in note.body.splitlines():
        cleaned = re.sub(str(rules["markdown_strip_pattern"]), " ", line).strip()
        if not cleaned:
            continue
        raw_segments.extend(re.split(str(rules["sentence_split_pattern"]), cleaned))
    minimum = int(config["min_segment_chars"])
    maximum = int(config["max_segment_chars"])
    unique: dict[str, None] = {}
    for segment in raw_segments:
        compact = re.sub(r"\s+", " ", segment).strip()
        if len(compact) < minimum:
            continue
        unique[compact[:maximum]] = None
    return list(unique)


def _extract_values(text: str, rules: dict[str, Any]) -> list[str]:
    values: set[str] = set()
    for pattern in cast("list[str]", rules["value_patterns"]):
        for match in re.finditer(pattern, text):
            value = match.group(0).strip(" `\"'.,:;()[]{}")
            if value:
                values.add(value)
    values.update(_matching_terms(text, cast("list[str]", rules["state_values"])))
    return sorted(values)


def _scope(text: str, rules: dict[str, Any], values: list[str]) -> str:
    scope_markers = cast("dict[str, list[str]]", rules["scope_markers"])
    ranked = [
        (len(_matching_terms(text, terms)), -index, scope)
        for index, (scope, terms) in enumerate(scope_markers.items())
    ]
    count, _, scope = max(ranked)
    return scope if count > 0 else (str(rules["value_scope"]) if values else "assertion")


def _polarity(text: str, rules: dict[str, Any]) -> str:
    negative = len(_matching_terms(text, cast("list[str]", rules["negative_markers"])))
    positive = len(_matching_terms(text, cast("list[str]", rules["positive_markers"])))
    if negative > positive:
        return "negative"
    if positive > negative:
        return "positive"
    return "neutral"


def _claims(note: Note, rules: dict[str, Any], config: dict[str, Any]) -> list[Claim]:
    stopwords = set(cast("list[str]", rules["stopwords"]))
    marker_vocabulary = set(cast("list[str]", rules["negative_markers"]))
    marker_vocabulary.update(cast("list[str]", rules["positive_markers"]))
    marker_vocabulary.update(cast("list[str]", rules["temporal_markers"]))
    for terms in cast("dict[str, list[str]]", rules["scope_markers"]).values():
        marker_vocabulary.update(terms)
    title_tokens = set(_tokens(_normalize(note.title, rules), rules))
    tag_tokens = {
        token
        for tag in note.tags
        for token in _tokens(_normalize(tag.replace("/", " "), rules), rules)
    }
    minimum_token_length = int(config["min_token_chars"])
    claims: list[Claim] = []
    for index, segment in enumerate(_segments(note, rules, config), 1):
        normalized = _normalize(segment, rules)
        values = _extract_values(normalized, rules)
        polarity = _polarity(normalized, rules)
        scope = _scope(normalized, rules, values)
        trigger_terms = cast("list[str]", rules["claim_trigger_terms"])
        if polarity == "neutral" and not values and not _matching_terms(normalized, trigger_terms):
            continue
        raw_subject = set(_tokens(normalized, rules)) | title_tokens | tag_tokens
        subject = sorted(
            token
            for token in raw_subject
            if len(token) >= minimum_token_length
            and token not in stopwords
            and token not in marker_vocabulary
            and not token.isdigit()
        )
        if not subject:
            continue
        temporal = sorted(_matching_terms(normalized, cast("list[str]", rules["temporal_markers"])))
        claims.append(
            Claim(
                claim_id=f"{note.note_id}:{index}",
                subject=tuple(subject),
                scope=scope,
                polarity=polarity,
                value_or_state=tuple(values),
                temporal_context=tuple(temporal),
                text=segment,
            )
        )
        if len(claims) >= int(config["max_claims_per_note"]):
            break
    return claims


def _claim_payload(claim: Claim) -> dict[str, Any]:
    return {
        "sujet": list(claim.subject),
        "portée": claim.scope,
        "polarité": claim.polarity,
        "valeur_ou_état": list(claim.value_or_state),
        "contexte_temporel": list(claim.temporal_context),
    }


def _compatible_scopes(scope_a: str, scope_b: str, config: dict[str, Any]) -> bool:
    if scope_a == scope_b:
        return True
    allowed = {tuple(sorted(map(str, pair))) for pair in config["compatible_scope_pairs"]}
    return tuple(sorted((scope_a, scope_b))) in allowed


def _conflicts(claim_a: Claim, claim_b: Claim, config: dict[str, Any]) -> list[str]:
    if not _compatible_scopes(claim_a.scope, claim_b.scope, config):
        return []
    conflicts: list[str] = []
    if {claim_a.polarity, claim_b.polarity} == {"negative", "positive"}:
        conflicts.append("polarity")
    values_a = set(claim_a.value_or_state)
    values_b = set(claim_b.value_or_state)
    if values_a and values_b and values_a != values_b:
        conflicts.append("value_or_state")
    return conflicts


def _subject_metrics(
    subject_a: frozenset[str],
    weight_a: float,
    subject_b: frozenset[str],
    weight_b: float,
    term_weights: dict[str, float],
) -> tuple[list[str], float, float]:
    shared = sorted(subject_a & subject_b)
    shared_weight = sum(term_weights[term] for term in shared)
    denominator = min(weight_a, weight_b)
    overlap = shared_weight / denominator if denominator else 0.0
    return shared, shared_weight, overlap


def _match_payload(
    claim_a: Claim,
    claim_b: Claim,
    shared: list[str],
    shared_weight: float,
    overlap: float,
    conflicts: list[str],
    score: float,
) -> dict[str, Any]:
    return {
        "score": round(score, 6),
        "shared_subjects": shared,
        "shared_subject_weight": round(shared_weight, 6),
        "subject_overlap": round(overlap, 6),
        "conflict_types": conflicts,
        "claim_a_id": claim_a.claim_id,
        "claim_b_id": claim_b.claim_id,
        "claim_a": _claim_payload(claim_a),
        "claim_b": _claim_payload(claim_b),
    }


def _all_candidates(notes: list[Note], config: dict[str, Any]) -> list[Candidate]:  # noqa: PLR0912, PLR0915
    scoring_config = {
        key: value for key, value in config.items() if key not in {"min_score", "top_n"}
    }
    cache_key = json.dumps(scoring_config, sort_keys=True, default=str)
    if _TRIAL_CACHE.get("key") == cache_key:
        return cast("list[Candidate]", _TRIAL_CACHE["candidates"])

    rules = _load_rules(config)
    claims_by_note = [_claims(note, rules, config) for note in notes]
    document_frequency: dict[str, int] = {}
    for claims in claims_by_note:
        note_terms = {term for claim in claims for term in claim.subject}
        for term in note_terms:
            document_frequency[term] = document_frequency.get(term, 0) + 1

    eligible_postings: dict[str, set[int]] = {}
    maximum_frequency = int(config["max_subject_note_frequency"])
    for note_index, claims in enumerate(claims_by_note):
        for term in {term for claim in claims for term in claim.subject}:
            if document_frequency[term] <= maximum_frequency:
                eligible_postings.setdefault(term, set()).add(note_index)
    possible_pairs: set[tuple[int, int]] = set()
    for postings in eligible_postings.values():
        possible_pairs.update(combinations(sorted(postings), 2))

    term_weights = {
        term: math.log((len(notes) + 1) / (frequency + 1)) + 1.0
        for term, frequency in document_frequency.items()
        if frequency <= maximum_frequency
    }
    features_by_note: list[list[tuple[frozenset[str], float]]] = []
    for claims in claims_by_note:
        note_features: list[tuple[frozenset[str], float]] = []
        for claim in claims:
            subject = frozenset(term for term in claim.subject if term in term_weights)
            note_features.append((subject, sum(term_weights[term] for term in subject)))
        features_by_note.append(note_features)

    candidates: list[Candidate] = []
    for index_a, index_b in sorted(possible_pairs):
        matches: list[dict[str, Any]] = []
        evaluations = 0
        for claim_a, features_a in zip(
            claims_by_note[index_a], features_by_note[index_a], strict=True
        ):
            for claim_b, features_b in zip(
                claims_by_note[index_b], features_by_note[index_b], strict=True
            ):
                shared, shared_weight, overlap = _subject_metrics(
                    features_a[0],
                    features_a[1],
                    features_b[0],
                    features_b[1],
                    term_weights,
                )
                if len(shared) < int(config["min_shared_subjects"]):
                    continue
                if shared_weight < float(config["min_shared_subject_weight"]):
                    continue
                if overlap < float(config["min_subject_overlap"]):
                    continue
                evaluations += 1
                if evaluations > int(config["max_claim_pair_evaluations"]):
                    break
                conflicts = _conflicts(claim_a, claim_b, config)
                if not conflicts:
                    continue
                conflict_weight = sum(float(config["conflict_weights"][kind]) for kind in conflicts)
                score = (
                    conflict_weight
                    + float(config["subject_overlap_weight"]) * overlap
                    + float(config["shared_subject_weight_factor"]) * shared_weight
                    + float(config["same_scope_bonus"]) * float(claim_a.scope == claim_b.scope)
                )
                matches.append(
                    _match_payload(
                        claim_a,
                        claim_b,
                        shared,
                        shared_weight,
                        overlap,
                        conflicts,
                        score,
                    )
                )
            if evaluations > int(config["max_claim_pair_evaluations"]):
                break
        if not matches:
            continue
        matches.sort(
            key=lambda item: (
                -float(item["score"]),
                str(item["claim_a_id"]),
                str(item["claim_b_id"]),
            )
        )
        retained = matches[: int(config["max_matches_per_pair"])]
        best = retained[0]
        conflict_types = sorted({kind for item in retained for kind in item["conflict_types"]})
        shared_subjects = sorted({term for item in retained for term in item["shared_subjects"]})
        candidates.append(
            Candidate(
                a=notes[index_a],
                b=notes[index_b],
                score=float(best["score"]),
                shared_subjects=shared_subjects,
                conflict_types=conflict_types,
                claim_matches=retained,
                reasons=[
                    f"claim_matches={len(matches)}",
                    f"best_conflicts={','.join(best['conflict_types'])}",
                    f"best_subject_overlap={best['subject_overlap']:.6f}",
                ],
            )
        )
    candidates.sort(key=lambda item: (-item.score, item.a.path, item.b.path))
    ranking_payload = [candidate_payload(candidate) for candidate in candidates]
    _TRIAL_METADATA.update(
        {
            "note_count": len(notes),
            "claim_count": sum(len(claims) for claims in claims_by_note),
            "claims_by_note_sha256": hashlib.sha256(
                json.dumps(
                    [[_claim_payload(claim) for claim in claims] for claims in claims_by_note],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "pool_pairs": len(candidates),
            "ranking_sha256": hashlib.sha256(
                json.dumps(
                    ranking_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
    )
    _TRIAL_CACHE.update({"key": cache_key, "candidates": candidates})
    return candidates


def score_pairs(
    notes: list[Note], config: dict[str, Any], *, include_supersedes: bool = False
) -> list[Candidate]:
    """Return the configured prefix of the deterministic claim-conflict ranking."""
    del include_supersedes
    candidates = _all_candidates(notes, config)
    threshold = float(config["min_score"])
    return [candidate for candidate in candidates if candidate.score >= threshold][
        : int(config["top_n"])
    ]


def candidate_payload(candidate: Candidate) -> dict[str, Any]:
    """Return deterministic, self-explanatory evidence for one candidate."""
    return {
        "score": round(candidate.score, 6),
        "a": candidate.a.path,
        "b": candidate.b.path,
        "shared_subjects": candidate.shared_subjects,
        "conflict_types": candidate.conflict_types,
        "claim_matches": candidate.claim_matches,
        "reasons": candidate.reasons,
    }


def signal_contributions(candidate: Candidate, config: dict[str, Any]) -> dict[str, float]:
    """Expose provider signals using the neutral harness convention."""
    del config
    best = candidate.claim_matches[0]
    return {
        "claim_conflict": float(best["score"]),
        "subject_overlap": float(best["subject_overlap"]),
    }
