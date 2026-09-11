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
"""Bounded, read-only session orientation with live source verification."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Final, Literal

from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN
from datacron.core.memory_protocol import (
    CONTRACT_HASH,
    CONTRACT_ID,
    CONTRACT_TEXT,
    CONTRACT_VERSION,
    SESSION_DOMAIN_TAGS,
    SESSION_MAX_NOTES,
    SESSION_MIN_TOKENS,
    SESSION_SUBJECT_CHARS,
)
from datacron.core.paths import PathConfinementError
from datacron.core.scope import NoteAdmissionError
from datacron.indexing.ripgrep import ripgrep_available
from datacron.mcp.tools.payloads import _audit, _error_response, _internal_error_response
from datacron.mcp.tools.read import _build_full_payload

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

SessionDomain = Literal["all", "project", "people", "meeting", "objective", "review"]

CONTEXT_BUDGET_TOO_SMALL: Final[str] = "context_budget_too_small"


class ContextBudgetError(ValueError):
    """The requested token budget cannot hold the memory contract.

    Carrying the refusal as a business exception routes it through the shared
    ``_error_response`` shape, so the MCP boundary emits a tool error instead of
    a result that the declared ``SessionContextOutput`` schema cannot describe.
    """

    code: Final[str] = CONTEXT_BUDGET_TOO_SMALL

    def __init__(self, required_tokens: int) -> None:
        super().__init__(f"session context requires at least {required_tokens} tokens")
        self.required_tokens = required_tokens


def _budget_refusal(started: float, rendered_chars: int) -> dict[str, Any]:
    """Return the typed refusal, preserving the measured requirement on every path."""
    required_tokens = -(-rendered_chars // TOKEN_ESTIMATE_CHARS_PER_TOKEN)
    payload = _error_response(
        "session_context",
        ContextBudgetError(required_tokens),
        started,
        required_tokens=required_tokens,
    )
    payload["error"]["required_tokens"] = required_tokens
    return payload


async def session_context(
    app: DatacronApp,
    *,
    subject: str | None = None,
    domain: SessionDomain = "all",
    note_paths: list[str] | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Return a complete protocol or explicit budget refusal, never mutate the index."""
    started = time.perf_counter()
    budget = min(max_tokens or app.settings.max_result_tokens, app.settings.max_result_tokens)
    if (
        (max_tokens is not None and max_tokens < SESSION_MIN_TOKENS)
        or (subject is not None and len(subject) > SESSION_SUBJECT_CHARS)
        or len(note_paths or []) > SESSION_MAX_NOTES
    ):
        return _error_response(
            "session_context", ValueError("session input exceeds bounds"), started
        )
    result: dict[str, Any] = {
        "contract": {
            "id": CONTRACT_ID,
            "version": CONTRACT_VERSION,
            "hash": CONTRACT_HASH,
            "instructions": CONTRACT_TEXT,
        },
        "capabilities": {
            "writes_enabled": app.write_policy.effective_writes_enabled,
            "reminders_scheduled": False,
            "regex_search_ripgrep": ripgrep_available(app.settings.ripgrep_path),
        },
        "evidence": "context_returned_not_behavior_verified",
        "sources": [],
        "unavailable": 0,
        "omitted": 0,
        "coverage": "selected_live_sources_only",
        "index_repaired": False,
        "identity": "not_resolved",
        "truncated": False,
    }
    maximum = budget * TOKEN_ESTIMATE_CHARS_PER_TOKEN
    kernel_size = rendered_size(result)
    if kernel_size > maximum:
        return _budget_refusal(started, kernel_size)
    try:
        paths = list(dict.fromkeys([*app.settings.session_context_paths, *(note_paths or [])]))
        if subject and subject.strip():
            # The store tokenizes plain text and implements its own AND/OR fallback.
            # Scoping by the domain tag keeps the bounded candidate list from being
            # consumed by notes that the domain filter would discard afterwards.
            domain_tag = SESSION_DOMAIN_TAGS.get(domain)
            hits = await app.store.search(
                subject,
                limit=app.settings.max_result_count,
                tags=[domain_tag] if domain_tag else None,
            )
            for hit in hits:
                if hit.chunk.note_rel_path not in paths and app.scope.allows_note_rel_path(
                    hit.chunk.note_rel_path
                ):
                    paths.append(hit.chunk.note_rel_path)
            result["coverage"] = "ranked_candidates_not_exhaustive"
        matched_people = await _load_sources(app, paths, note_paths, domain, result)
        result["identity"] = "clarification_required" if matched_people > 1 else "not_resolved"
        while rendered_size(result) > maximum and result["sources"]:
            result["sources"].pop()
            result["omitted"] += 1
        result["truncated"] = bool(
            result["omitted"] or any(x["truncated"] for x in result["sources"])
        )
        # Account for digit growth in counters before accepting the final serialization.
        final_size = rendered_size(result)
        if final_size > maximum:
            return _budget_refusal(started, final_size)
        _audit(
            "session_context",
            started,
            returned=len(result["sources"]),
            truncated=result["truncated"],
            contract_version=CONTRACT_VERSION,
        )
        return result
    except Exception:
        return _internal_error_response("session_context", started)


def rendered_size(payload: object) -> int:
    """Use conservative ASCII JSON accounting including all outer fields."""
    return len(json.dumps(payload, ensure_ascii=True, indent=2))


async def _load_sources(
    app: DatacronApp,
    paths: list[str],
    note_paths: list[str] | None,
    domain: SessionDomain,
    result: dict[str, Any],
) -> int:
    matched_people = 0
    for path in paths:
        if len(result["sources"]) >= SESSION_MAX_NOTES:
            result["omitted"] += 1
            continue
        try:
            note = await app.vault_reader.read_note(app.scope.authorize_note_rel_path(path))
        except (ValueError, FileNotFoundError, NoteAdmissionError, PathConfinementError):
            result["unavailable"] += 1
            continue
        if path not in app.settings.session_context_paths and path not in (note_paths or []):
            tag = SESSION_DOMAIN_TAGS.get(domain)
            if tag and tag not in note.tags:
                continue
        if "memory/contact" in note.tags:
            matched_people += 1
        full = _build_full_payload(app, note, offset=0, limit=app.settings.session_note_chars)
        item = {
            key: full[key]
            for key in (
                "id",
                "rel_path",
                "title",
                "content_hash",
                "content",
                "next_offset",
                "truncated",
            )
        }
        item["next_read"] = {
            "tool": "get_note",
            "id_or_path": full["rel_path"],
            "offset": full["next_offset"] or 0,
            "format": "full",
        }
        result["sources"].append(item)
    return matched_people
