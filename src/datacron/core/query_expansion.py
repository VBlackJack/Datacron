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
"""Query-time term expansion for curated bilingual vault vocabulary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

__all__ = [
    "DEFAULT_QUERY_EXPANSION",
    "default_query_expansion",
    "expand_terms",
    "normalize_term_map",
    "query_expansion_seed",
]

# V1 is token-level only: callers expand terms after the FTS tokenizer has
# split the query, so multi-word and hyphenated phrases such as "at rest" or
# "pare-feu" are intentionally outside matching until phrase-level expansion is
# designed.
DEFAULT_QUERY_EXPANSION: Final[dict[str, tuple[str, ...]]] = {
    "supervision": ("monitoring",),
    "sauvegarde": ("backup",),
    "restauration": ("restore",),
    "chiffrement": ("encryption",),
    "chiffré": ("encrypted",),
    "durcissement": ("hardening",),
    "journalisation": ("logging",),
    "pare-feu": ("firewall",),
    "ordonnancement": ("scheduling",),
    "glossaire": ("glossary",),
    "disponibilité": ("availability",),
    "sécurité": ("security",),
    "validité": ("validity",),
    "certificat": ("certificate",),
}


def default_query_expansion() -> dict[str, list[str]]:
    """Return the default expansion map, normalized and bidirectional."""
    return normalize_term_map(DEFAULT_QUERY_EXPANSION)


def query_expansion_seed() -> dict[str, list[str]]:
    """Return the human-authored default map for new vault config files."""
    return {term: list(equivalents) for term, equivalents in DEFAULT_QUERY_EXPANSION.items()}


def normalize_term_map(term_map: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Normalize and close a one-way term map into a bidirectional lookup."""
    normalized: dict[str, list[str]] = {}
    for raw_term, raw_equivalents in term_map.items():
        term = _normalize_term(raw_term)
        if not term:
            continue
        for raw_equivalent in raw_equivalents:
            equivalent = _normalize_term(raw_equivalent)
            if not equivalent:
                continue
            _append_equivalent(normalized, term, equivalent)
            _append_equivalent(normalized, equivalent, term)
    return normalized


def expand_terms(
    terms: list[str],
    term_map: Mapping[str, Sequence[str]],
) -> list[list[str]]:
    """Each term becomes a deduplicated group ``[term, *equivalents]``.

    Lookup is case-insensitive. The input term stays first and keeps its
    original spelling; configured equivalents are normalized to lowercase.
    """
    lookup = normalize_term_map(term_map)
    groups: list[list[str]] = []
    for raw_term in terms:
        term = str(raw_term).strip()
        if not term:
            continue
        seen = {term.lower()}
        group = [term]
        for equivalent in lookup.get(term.lower(), []):
            if equivalent in seen:
                continue
            group.append(equivalent)
            seen.add(equivalent)
        groups.append(group)
    return groups


def _normalize_term(term: object) -> str:
    return str(term).strip().lower()


def _append_equivalent(term_map: dict[str, list[str]], term: str, equivalent: str) -> None:
    if term == equivalent:
        return
    equivalents = term_map.setdefault(term, [])
    if equivalent not in equivalents:
        equivalents.append(equivalent)
