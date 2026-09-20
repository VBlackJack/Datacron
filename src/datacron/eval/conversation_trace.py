# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Deterministic grading of exported conversation traces, independent of a provider."""

from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from datacron.mcp.security_manifest import MUTATING_TOOL_NAMES


class ConversationCase(BaseModel):
    """Explicit acceptance criteria; literal checks do not certify semantic truth."""

    model_config = ConfigDict(extra="forbid")
    name: str
    turns: list[str] = Field(default_factory=list)
    minimum_sessions: int = Field(default=1, ge=1)
    required_tools: dict[str, int] = Field(default_factory=dict)
    required_final_text: list[str] = Field(default_factory=list)
    forbidden_final_text: list[str] = Field(default_factory=list)
    cited_paths: list[str] = Field(default_factory=list)
    verify_writes: bool = True


class TraceEvent(BaseModel):
    """One observed tool exchange or final assistant answer with session identity."""

    model_config = ConfigDict(extra="forbid")
    session: str = Field(min_length=1)
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    answer: str | None = None


def grade(case: ConversationCase, events: list[TraceEvent]) -> dict[str, Any]:
    """Check declared outcomes, successful source reads and post-write verification."""
    calls = Counter(e.tool for e in events if e.tool and "error" not in e.result)
    final = next((e.answer for e in reversed(events) if e.answer is not None), "") or ""
    failures = [
        *_session_failures(case, events),
        *_tool_failures(case, calls),
        *_final_text_failures(case, final),
        *_citation_failures(case, events, final),
    ]
    if case.verify_writes:
        failures.extend(_write_failures(events))
    return {
        "case": case.name,
        "passed": not failures,
        "failures": failures,
        "sessions": len({e.session for e in events}),
        "tool_calls": sum(calls.values()),
        "evidence": "supplied_trace_and_literal_oracles_not_semantic_truth",
    }


def _session_failures(case: ConversationCase, events: list[TraceEvent]) -> list[str]:
    if len({event.session for event in events}) < case.minimum_sessions:
        return ["insufficient_sessions"]
    return []


def _tool_failures(case: ConversationCase, calls: Counter[str]) -> list[str]:
    return [
        f"missing_tool:{tool}"
        for tool, minimum in case.required_tools.items()
        if calls[tool] < minimum
    ]


def _final_text_failures(case: ConversationCase, final: str) -> list[str]:
    failures: list[str] = []
    if not final.strip():
        failures.append("missing_final_answer")
    folded = final.casefold()
    failures.extend(
        f"missing_final_text:{text}"
        for text in case.required_final_text
        if text.casefold() not in folded
    )
    failures.extend(
        f"forbidden_final_text:{text}"
        for text in case.forbidden_final_text
        if text.casefold() in folded
    )
    return failures


def _citation_failures(case: ConversationCase, events: list[TraceEvent], final: str) -> list[str]:
    read_paths = {
        str(e.result.get("rel_path"))
        for e in events
        if e.tool == "get_note"
        and "error" not in e.result
        and e.result.get("content_hash")
        and e.result.get("content")
    }
    return [
        f"unverified_citation:{path}"
        for path in case.cited_paths
        if path not in read_paths or path not in final
    ]


_BATCH_WRITE_TOOLS: frozenset[str] = frozenset({"apply_organization_manifest"})
"""Mutating tools that do not write one note at a given ``rel_path``.

A manifest apply moves and rewrites many notes at once and takes no ``rel_path``
argument at all, so the per-note re-read rule below could never be satisfied and
every trace containing one was graded as a failed write. Its receipt carries what
there is to verify: the batch committed and the report it produced matches.
"""


def _batch_write_failure(index: int, event: TraceEvent) -> str | None:
    """Check a batch apply against its own receipt rather than against a path."""
    result = event.result
    if result.get("already_committed") is True:
        return None
    if result.get("status") != "applied" or result.get("indexed") is not True:
        return f"batch_not_applied:{index}"
    if result.get("committed_error_code") is not None:
        return f"batch_committed_error:{index}"
    projected = result.get("projected_report_sha256")
    if projected is not None and projected != result.get("final_report_sha256"):
        return f"batch_report_mismatch:{index}"
    return None


def _write_failures(events: list[TraceEvent]) -> list[str]:
    failures: list[str] = []
    for index, event in enumerate(events):
        if event.tool not in MUTATING_TOOL_NAMES or "error" in event.result:
            continue
        if event.tool in _BATCH_WRITE_TOOLS:
            batch_failure = _batch_write_failure(index, event)
            if batch_failure is not None:
                failures.append(batch_failure)
            continue
        if not event.result.get("replayed") and event.result.get("indexed") is not True:
            failures.append(f"unconfirmed_index:{index}")
            continue
        target = event.arguments.get("rel_path")
        content_hash = event.result.get("content_hash")
        if (
            not target
            or not content_hash
            or not any(
                later.tool == "get_note"
                and later.result.get("rel_path") == target
                and (
                    event.result.get("replayed") or later.result.get("content_hash") == content_hash
                )
                and "error" not in later.result
                and bool(later.result.get("content"))
                for later in events[index + 1 :]
            )
        ):
            failures.append(f"write_not_reread:{index}")
    return failures
