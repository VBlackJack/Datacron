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
"""Preview and commit an exact single-note section relocation."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Final

from datacron.core.durability import DurabilityUnavailableError, ReadOnlyModeError
from datacron.core.frontmatter import FrontmatterError
from datacron.core.hashing import sha256_bytes
from datacron.core.markdown_sections import (
    SectionSelector,
    SectionSelectorError,
    move_note_section,
)
from datacron.core.operation_log import OperationContext
from datacron.core.scope import authorize_note_write
from datacron.core.vault_writer import WriteConflictError
from datacron.mcp.tools.payloads import _audit
from datacron.mcp.tools.read import _read_note_by_rel_path
from datacron.mcp.tools.write import _execute_write_tool, _reconcile_committed_write
from datacron.mcp.tools.write_requests import replayable_write
from datacron.mcp.tools.write_validation import (
    HEADING_LEVELS,
    _parse_preserving_bom_and_body_eols,
    _validate_expected_hash,
    _validate_heading_occurrence,
)

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

__all__ = ["_move_note_section_impl"]


# The shared validator speaks of the source parameters; a destination has its own names.
_DESTINATION_PARAMETERS: Final[dict[str, str]] = {
    "heading_occurrence": "destination_occurrence",
    "heading_level": "destination_level",
    "heading ": "destination heading ",
}


def _selector(
    heading: str,
    level: int | None,
    occurrence: int | None,
    expected_hash: str,
    selector: SectionSelector = "source",
) -> str:
    try:
        if not heading.strip() or "\n" in heading or "\r" in heading:
            raise ValueError("heading must contain nonempty single-line text")
        if level is not None and (type(level) is not int or level not in HEADING_LEVELS):
            raise ValueError("heading level must be an integer between 1 and 6")
        _validate_heading_occurrence(occurrence, heading_level=level, expected_hash=expected_hash)
    except ValueError as exc:
        if selector == "source":
            raise
        message = str(exc)
        for source_name, destination_name in _DESTINATION_PARAMETERS.items():
            message = message.replace(source_name, destination_name)
        raise SectionSelectorError(message, selector=selector) from exc
    return heading.strip()


@replayable_write
async def _move_note_section_impl(
    app: DatacronApp,
    *,
    rel_path: str,
    heading: str,
    destination_heading: str,
    expected_hash: str | None = None,
    heading_level: int | None = None,
    heading_occurrence: int | None = None,
    destination_level: int | None = None,
    destination_occurrence: int | None = None,
    confirm: bool = False,
    actor: str = "direct-call",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Preview by default; commit an explicitly confirmed exact-CAS section move."""
    started = time.perf_counter()
    # Remembered so the error payload can say which selector failed to pick a section.
    failed_selector: SectionSelector | None = None

    async def action() -> dict[str, Any]:
        nonlocal failed_selector
        app.write_policy.ensure_writable()
        if type(confirm) is not bool:
            raise ValueError("confirm must be a boolean")
        cleaned_hash = _validate_expected_hash(expected_hash)
        if cleaned_hash is None:
            raise ValueError("expected_hash is required")
        cleaned_path = rel_path.strip()
        if not cleaned_path.endswith(".md"):
            raise ValueError("rel_path must end with .md")
        try:
            cleaned_heading = _selector(
                heading, heading_level, heading_occurrence, cleaned_hash, "source"
            )
            cleaned_destination = _selector(
                destination_heading,
                destination_level,
                destination_occurrence,
                cleaned_hash,
                "destination",
            )
        except SectionSelectorError as exc:
            failed_selector = exc.selector
            raise
        # The same gate as the scoped writer, before the preview reads anything, so a
        # preview and a commit refuse an excluded or linked note identically.
        authorize_note_write(app.scope, cleaned_path)
        selection: dict[str, int] = {}

        def mutation(raw: str) -> str:
            nonlocal selection, failed_selector
            # The durable writer normalizes EOLs. Refuse inputs where that would
            # change any original byte beyond the requested relocation.
            without_crlf = raw.replace("\r\n", "")
            if "\r" in without_crlf or ("\r\n" in raw and "\n" in without_crlf):
                raise ValueError("mixed or bare-CR line endings cannot be preserved exactly")
            _, body, _ = _parse_preserving_bom_and_body_eols(raw)
            if not raw.endswith(body):
                raise ValueError(
                    "Markdown body cannot be separated without changing original bytes"
                )
            try:
                moved, selection = move_note_section(
                    body,
                    cleaned_heading,
                    cleaned_destination,
                    heading_level=heading_level,
                    heading_occurrence=heading_occurrence,
                    destination_level=destination_level,
                    destination_occurrence=destination_occurrence,
                    source_context=raw,
                )
            except SectionSelectorError as exc:
                failed_selector = exc.selector
                raise
            # Preserve frontmatter, BOM, comments and timestamps verbatim.
            return raw[: len(raw) - len(body)] + moved

        if not confirm:
            note = await _read_note_by_rel_path(app, cleaned_path)
            if note.content_hash != cleaned_hash:
                raise WriteConflictError("expected_hash does not match current note bytes")
            projected = mutation(note.raw_content)
            _audit("move_note_section", started, rel_path=cleaned_path, confirmed=False)
            return {
                "rel_path": cleaned_path,
                "selection": selection,
                "before_hash": cleaned_hash,
                "projected_hash": sha256_bytes(projected.encode("utf-8")),
                "committed": False,
            }
        parameters: dict[str, Any] = {
            "heading": cleaned_heading,
            "destination_heading": cleaned_destination,
            "heading_level": heading_level,
            "heading_occurrence": heading_occurrence,
            "destination_level": destination_level,
            "destination_occurrence": destination_occurrence,
        }
        content_hash = await app.vault_writer.mutate_note_atomic(
            cleaned_path,
            mutation,
            expected_hash=cleaned_hash,
            operation=OperationContext(
                op="move_section", tool="move_note_section", actor=actor, parameters=parameters
            ),
        )
        await _reconcile_committed_write(app, content_hash, cleaned_path)
        _audit("move_note_section", started, rel_path=cleaned_path, confirmed=True)
        return {
            "rel_path": cleaned_path,
            "selection": selection,
            "before_hash": cleaned_hash,
            "content_hash": content_hash,
            "committed": True,
            "indexed": True,
        }

    payload = await _execute_write_tool(
        "move_note_section",
        started,
        action,
        app=app,
        audit_fields={"rel_path": rel_path},
        expected=(
            DurabilityUnavailableError,
            ReadOnlyModeError,
            FileNotFoundError,
            FrontmatterError,
            ValueError,
        ),
    )
    if failed_selector is not None and "error" in payload:
        payload["error"]["selector"] = failed_selector
    return payload
