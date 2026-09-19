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
"""Read-only contradiction scan, elicitation, and confirmation surface."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal

from mcp.server.mcpserver import Context
from pydantic import BaseModel, ConfigDict

from datacron.contradictions import (
    Candidate,
    CandidateClass,
    MutationScope,
    build_proposal,
    build_scan_report,
    confirm_proposal,
)
from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN
from datacron.mcp.tools.payloads import _audit, _error_response, _internal_error_response
from datacron.mcp.tools.search import _repair_index_on_read
from datacron.mcp.tools.session import rendered_size

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

__all__ = ["_contradiction_scan_impl"]

ScanMode = Literal["scan", "confirm"]
ScanDetail = Literal["summary", "full"]


class ContradictionDecision(BaseModel):
    """Primitive-only MCP elicitation form for one candidate."""

    model_config = ConfigDict(extra="forbid")

    classification: Literal["CONTRADICTION", "RAFFINEMENT", "QUESTION_OUVERTE"] = "RAFFINEMENT"
    scope: Literal["section", "whole_note"] = "section"


async def _contradiction_scan_impl(
    app: DatacronApp,
    *,
    mode: ScanMode = "scan",
    detail: ScanDetail = "summary",
    proposal_token: str | None = None,
    ctx: Context[Any, Any] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Scan or confirm without ever invoking a mutating tool."""
    started = time.perf_counter()
    try:
        if mode == "confirm":
            if proposal_token is None or not proposal_token.strip():
                raise ValueError("proposal_token is required in confirm mode")
            payload = _within_budget(app, await confirm_proposal(app, proposal_token.strip()))
            _audit(
                "contradiction_scan",
                started,
                mode=mode,
                confirmed=True,
                writes="none",
            )
            return payload
        if mode != "scan":
            raise ValueError("mode must be 'scan' or 'confirm'")
        if detail not in {"summary", "full"}:
            raise ValueError("detail must be 'summary' or 'full'")
        if proposal_token is not None:
            raise ValueError("proposal_token is only valid in confirm mode")

        # Repair stats are intentionally not echoed in the payload: consecutive
        # scans on the same index must be byte-identical, and repair activity
        # is run-dependent state (the first call may repair, the second not).
        await _repair_index_on_read(app)
        payload, candidates = await build_scan_report(app, today=today, detail=detail)
        if _client_supports_form_elicitation(ctx):
            elicited = await _elicit_first_candidate(
                app,
                ctx=ctx,
                candidates=candidates,
                today=today,
            )
            if elicited is not None:
                _audit(
                    "contradiction_scan",
                    started,
                    mode=mode,
                    candidate_count=payload["candidate_count"],
                    elicitation=elicited.get("elicitation_action", "accept"),
                    writes="none",
                )
                if elicited.get("mode") == "confirm":
                    return _within_budget(app, elicited)
                payload["elicitation_action"] = elicited["elicitation_action"]

        _audit(
            "contradiction_scan",
            started,
            mode=mode,
            candidate_count=payload["candidate_count"],
            examined_pairs=payload["examined_pairs"],
            writes="none",
        )
        return payload
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return _error_response(
            "contradiction_scan",
            exc,
            started,
            mode=mode,
            writes="none",
        )
    except Exception:
        return _internal_error_response("contradiction_scan", started, mode=mode, writes="none")


async def _elicit_first_candidate(
    app: DatacronApp,
    *,
    ctx: Context[Any, Any] | None,
    candidates: list[Candidate],
    today: date | None,
) -> dict[str, Any] | None:
    if ctx is None:
        return None
    candidate = next((item for item in candidates if item.addressable), None)
    if candidate is None:
        return None

    result = await ctx.elicit(
        message=(
            "Review one contradiction candidate. The safe default is RAFFINEMENT at "
            f"section scope; suggested class: {candidate.classification.value}."
        ),
        schema=ContradictionDecision,
    )
    if result.action != "accept":
        return {"elicitation_action": result.action}

    proposal_date = today or datetime.now(tz=UTC).date()
    classification = CandidateClass(result.data.classification)
    scope = MutationScope(result.data.scope)
    proposal = build_proposal(
        candidate,
        classification=classification,
        scope=scope,
        today=proposal_date,
    )
    return await confirm_proposal(app, proposal.token)


class ContradictionBudgetError(ValueError):
    """Raised when a confirmation would not fit the caller's result budget."""


def _within_budget(app: DatacronApp, payload: dict[str, Any]) -> dict[str, Any]:
    """Return the confirmation, or refuse it for being larger than the budget allows.

    A confirmation carries the write call the caller is meant to execute, and that
    call carries the section's new content: the whole live section plus the block to
    append, byte for byte, with no excerpt limit. A long running journal section is
    measured in hundreds of kilobytes, and the transport serialises a result twice,
    once as text and once as structured content, so one confirmation could take the
    caller's whole context.

    Every other tool that returns vault bytes measures itself against the same
    budget; this one did not. Refusing is the right answer rather than truncating,
    because the payload is an exact write call and a truncated one would corrupt the
    note it is applied to.
    """
    budget = app.settings.max_result_tokens * TOKEN_ESTIMATE_CHARS_PER_TOKEN
    size = rendered_size(payload)
    if size <= budget:
        return payload
    raise ContradictionBudgetError(
        f"the confirmation renders to {size} characters against a budget of {budget}; "
        "target a lower-level heading so the section it rewrites is smaller, or raise "
        "DATACRON_MAX_RESULT_TOKENS"
    )


def _client_supports_form_elicitation(ctx: Context[Any, Any] | None) -> bool:
    """Return whether the initialized client declared form elicitation."""
    if ctx is None:
        return False
    if not ctx.session.can_send_request:
        return False
    client_params = ctx.session.client_params
    if client_params is None or client_params.capabilities.elicitation is None:
        return False
    return client_params.capabilities.elicitation.form is not None
