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
"""The runbook capture: file one durable fact in the right runbook and section.

The capture is composed from parts that already exist or are pure: routing
(``organization.runbooks``), rendering (``organization.runbook_entries``), the
write boundary (``core.runbook_scope``) and a writer confined to
``DATACRON_RUNBOOK_WRITE_PATHS`` that shares the ordinary writer's locks,
operation journal and recovery. It never adds a frontmatter block to a file
that has none, never writes ``origin`` or ``confidence``, and never runs git.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from datacron.core.case_folding import filesystem_folds_case
from datacron.core.config import (
    RUNBOOK_EXCLUDED_DIRECTORIES,
    RUNBOOK_MARKDOWN_SUFFIX,
    TOOL_PROFILE_ESSENTIAL,
    RunbooksConfig,
    RunbookTemplate,
)
from datacron.core.durability import DurabilityUnavailableError, ReadOnlyModeError
from datacron.core.frontmatter import (
    FrontmatterError,
    has_ambiguous_leading_delimiter_block,
    parse_preserving_bom_and_body_eols,
)
from datacron.core.markdown_headings import markdown_headings, split_markdown_lines
from datacron.core.markdown_sections import append_entry_to_heading
from datacron.core.markdown_tables import section_own_text, upsert_table_row
from datacron.core.operation_log import OperationContext
from datacron.core.paths import PathConfinementError, assert_within_paths
from datacron.core.runbook_scope import RunbookScopeError, admit_runbook_write
from datacron.core.vault_writer import WriteConflictError
from datacron.core.write_request import ACTIVE_WRITE_REQUEST, ReplayedWriteError
from datacron.mcp.tools.payloads import _audit, _error_response, _internal_error_response
from datacron.mcp.tools.write import _execute_write_tool, _reconcile_committed_write
from datacron.mcp.tools.write_requests import replayable_write
from datacron.mcp.tools.write_validation import _serialize_preserving_frontmatter
from datacron.organization.runbook_entries import (
    RUNBOOK_EVIDENCE,
    RUNBOOK_EVIDENCE_MEASURED,
    RUNBOOK_LINE_FIELD_MAX_CHARS,
    RunbookEntry,
    RunbookEntryError,
    entry_key,
    render_entry,
    render_update,
)
from datacron.organization.runbooks import (
    RunbookFile,
    RunbookLocation,
    RunbookRoutingError,
    resolve_role,
    resolve_role_file,
    resolve_runbook,
)

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

__all__ = [
    "RUNBOOK_SERVER_INSTRUCTIONS",
    "RUNBOOK_TOOL_NAME",
    "_runbook_capture_impl",
    "register_runbook_tools",
    "runbook_capture_offered",
]

RUNBOOK_TOOL_NAME: Final[str] = "runbook_capture"

ACTION_CREATED: Final[str] = "created"
ACTION_APPENDED: Final[str] = "appended"
ACTION_UPDATED: Final[str] = "updated"
ACTION_SKIPPED: Final[str] = "duplicate_skipped"

_OP_CREATE: Final[str] = "create"
_OP_APPEND: Final[str] = "append"
_BOM: Final[str] = "\ufeff"
_PHASE_FIELD: Final[str] = "phase"
_COMMAND_KIND: Final[str] = "command"
_LAST_VERIFIED: Final[str] = "last_verified"
_UPDATED: Final[str] = "updated"
_REQUEST_FINGERPRINT_PARAMETER: Final[str] = "request_fingerprint"
_REFUSAL_REQUEST_REUSED: Final[str] = "request_id was already used with different arguments"
_REFUSAL_NOT_CONFIGURED: Final[str] = (
    "runbook capture is not configured: declare 'runbooks' in .datacron/VAULT.yaml and "
    "set DATACRON_RUNBOOK_WRITE_PATHS"
)
_PROVENANCE: Final[re.Pattern[str]] = re.compile(
    r"\(?\b(?:" + "|".join(RUNBOOK_EVIDENCE) + r") \d{4}-\d{2}-\d{2}\b\)?"
)
_TABLE_OUTCOMES: Final[dict[str, str]] = {
    "created": ACTION_APPENDED,
    "appended": ACTION_APPENDED,
    "replaced": ACTION_UPDATED,
    "unchanged": ACTION_SKIPPED,
}

RUNBOOK_CAPTURE_DESCRIPTION: Final[str] = (
    "File one durable fact in the subject's runbook as soon as work establishes it: a "
    "command that ran, an error and its fix, a server name, an escalation contact, a "
    "decision. Say how it is known: measured (a command actually run and its real "
    "output), said (a dated meeting or a named person) or unverified; never capture a "
    "guess, a plausible step or a value you did not see. 'subject' is a registered "
    "subject, an existing runbook or a runbook folder path; the kind routes the entry to "
    "its file and section, and a refusal lists the candidates instead of guessing. A known "
    "entry is updated or skipped, never added twice. A field that looks like a secret is "
    "refused. Writes are confined to DATACRON_RUNBOOK_WRITE_PATHS, journaled and never "
    "committed to git. dry_run returns the plan without writing."
)


# Appended to the standard server instructions only when the capture is offered.
# It lives here and not in the memory contract: the contract is what clients
# install into their rule files, and Windsurf's 6000-character budget leaves it
# no room. Server instructions are delivered by the server and cost no rule file.
RUNBOOK_SERVER_INSTRUCTIONS: Final[str] = (
    "Runbooks: this vault keeps one runbook per subject. As soon as work establishes a "
    "durable fact (a command that ran, an error and its fix, a server name, an escalation "
    "contact, a decision), call runbook_capture in the same turn: do not wait for the end "
    "of the session or for a request. Say how it is known: measured (a command actually "
    "run and its real output), said (a dated meeting or a named person), or unverified. "
    "Never capture a guess, a plausible step, a secret or a value you did not see. A known "
    "entry is updated, not added again. A refusal lists the candidates: pick one, never "
    "guess a section."
)


def runbook_capture_offered(app: DatacronApp) -> bool:
    """Return whether this server registers ``runbook_capture``."""
    return (
        app.settings.tool_profile != TOOL_PROFILE_ESSENTIAL
        and not app.settings.read_only
        and app.runbooks is not None
        and app.runbook_writer is not None
    )


class _NothingToWriteError(Exception):
    """The entry is already present: the locked mutation must not write."""


def _today() -> date:
    return datetime.now(tz=UTC).date()


def _observed(value: str | None) -> date:
    if value is None or not value.strip():
        return _today()
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise RunbookEntryError(f"observed_on must be a YYYY-MM-DD date; got {value!r}") from exc
    if parsed > _today():
        raise RunbookEntryError(f"observed_on must not be in the future; got {value!r}")
    return parsed


def _source(value: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise RunbookEntryError("source must not be empty")
    if "\n" in text or "\r" in text:
        raise RunbookEntryError("source must be a single line")
    if len(text) > RUNBOOK_LINE_FIELD_MAX_CHARS:
        raise RunbookEntryError(f"source must be at most {RUNBOOK_LINE_FIELD_MAX_CHARS} characters")
    return text


def _readable_directory(app: DatacronApp, path: Path) -> bool:
    if not path.is_dir():
        return False
    if not app.settings.read_paths:
        return True
    try:
        assert_within_paths(path, app.settings.read_paths, kind="read")
    except PathConfinementError:
        return False
    return True


def _child_folders(app: DatacronApp, runbooks: RunbooksConfig) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for root in runbooks.roots:
        folder = app.vault_root / root.folder
        if not _readable_directory(app, folder):
            children[root.folder] = []
            continue
        children[root.folder] = sorted(
            entry.name
            for entry in os.scandir(folder)
            if entry.is_dir(follow_symlinks=False)
            and entry.name.casefold() not in RUNBOOK_EXCLUDED_DIRECTORIES
        )
    return children


def _file_stems(app: DatacronApp, location: RunbookLocation) -> list[str]:
    folder = app.vault_root / location.directory
    if not location.exists or not _readable_directory(app, folder):
        return []
    suffix = RUNBOOK_MARKDOWN_SUFFIX
    return sorted(
        entry.name[: -len(suffix)]
        for entry in os.scandir(folder)
        if entry.is_file(follow_symlinks=False) and entry.name.casefold().endswith(suffix)
    )


def _without_provenance(text: str) -> str:
    """Drop every provenance mark, so the same fact seen on another day still matches."""
    return " ".join(_PROVENANCE.sub(" ", text).split())


def _already_present(entry_body: str, section: str) -> bool:
    """Return whether the entry's text, provenance aside, is already in the section."""
    wanted = entry_key(_without_provenance(entry_body))
    return bool(wanted) and wanted in entry_key(_without_provenance(section))


def _known_heading(body: str, heading: str) -> str:
    """Return the existing heading the entry's heading names, spelled as the file spells it.

    Headings compare like entry keys, so ``a client stops updating`` finds
    ``A client stops updating`` instead of starting a second section for it.
    """
    wanted = entry_key(heading)
    for item in markdown_headings(split_markdown_lines(body)):
        if entry_key(item.text) == wanted:
            return item.text
    return heading


def _apply_body(
    body: str,
    entry: RunbookEntry,
    *,
    evidence: str,
    observed_on: date,
    template: RunbookTemplate,
    app: DatacronApp,
) -> tuple[str, str]:
    entry = replace(entry, heading=_known_heading(body, entry.heading))
    if entry.row is not None and entry.header is not None:
        rendered, outcome = upsert_table_row(body, entry.heading, entry.row, entry.header)
        return rendered, _TABLE_OUTCOMES[outcome]
    section = section_own_text(body, entry.heading)
    if section is not None and _already_present(entry.body, section):
        return body, ACTION_SKIPPED
    if entry.owns_heading and section is not None:
        update = render_update(
            _without_provenance(entry.body),
            evidence=evidence,
            observed_on=observed_on,
            template=template,
            redactor=app.secret_redactor,
        )
        return append_entry_to_heading(body, entry.heading, update), ACTION_UPDATED
    return append_entry_to_heading(body, entry.heading, entry.body), ACTION_APPENDED


def _apply(
    raw: str,
    entry: RunbookEntry,
    *,
    evidence: str,
    observed_on: date,
    template: RunbookTemplate,
    app: DatacronApp,
) -> tuple[str, str]:
    """Return the file after the capture and what the capture did to it.

    A file without frontmatter keeps none. A file with one gets ``last_verified``
    only from a measured capture, and ``updated`` only when it already has the key.
    """
    if has_ambiguous_leading_delimiter_block(raw):
        raise RunbookEntryError(
            "the file opens with a --- block that is not frontmatter; it cannot be edited safely"
        )
    metadata, body, has_bom = parse_preserving_bom_and_body_eols(raw)
    new_body, action = _apply_body(
        body, entry, evidence=evidence, observed_on=observed_on, template=template, app=app
    )
    if action == ACTION_SKIPPED:
        return raw, action
    head = raw[: len(raw) - len(body)] if raw.endswith(body) else raw
    if not head.removeprefix(_BOM):
        return (_BOM if has_bom else "") + new_body, action
    changed = dict(metadata)
    if evidence == RUNBOOK_EVIDENCE_MEASURED:
        changed[_LAST_VERIFIED] = observed_on.isoformat()
    if _UPDATED in metadata:
        changed[_UPDATED] = datetime.now(tz=UTC).isoformat()
    return _serialize_preserving_frontmatter(raw, changed, new_body, has_bom=has_bom), action


def _read_exact(path: Path) -> str:
    """Read a file with its line ends untouched, as the writer's mutation sees it."""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _new_file(template: RunbookTemplate, role: str) -> str:
    title = template.roles[role].title
    if title is None:
        raise RunbookRoutingError(
            f"role {role!r} declares no title, so its file cannot be created; add 'title' to "
            "the role in VAULT.yaml or create the file first"
        )
    return f"# {title}\n"


async def _journal_replay(app: DatacronApp) -> dict[str, Any] | None:
    """Answer a retried request from its journal record before anything is resolved."""
    request = ACTIVE_WRITE_REQUEST.get()
    if request is None:
        return None
    record = await app.vault_writer.latest_record_for_request(request.key_hash)
    if record is None:
        return None
    if record.parameters.get(_REQUEST_FINGERPRINT_PARAMETER) != request.fingerprint:
        raise WriteConflictError(_REFUSAL_REQUEST_REUSED)
    request.record = record
    return {
        "rel_path": record.rel_path,
        "operation_id": record.operation_id,
        "content_hash": record.after_hash,
        "indexed": False,
        "action": str(record.parameters.get("action", "")),
        "replayed": True,
    }


async def _latest_operation_id(app: DatacronApp, rel_path: str, content_hash: str) -> str | None:
    try:
        record = await app.vault_writer.latest_record_for_path(rel_path)
    except Exception:
        return None
    if record is not None and record.after_hash == content_hash:
        return record.operation_id
    return None


def _plan(
    app: DatacronApp,
    runbooks: RunbooksConfig,
    *,
    subject: str,
    kind: str,
    target_role: str | None,
    fields: Mapping[str, object],
) -> tuple[RunbookLocation, RunbookFile]:
    fold_case = filesystem_folds_case(app.vault_root)
    location = resolve_runbook(
        subject, runbooks, app.organization, _child_folders(app, runbooks), fold_case=fold_case
    )
    explicit = target_role
    if explicit is None and kind == _COMMAND_KIND and isinstance(fields.get(_PHASE_FIELD), str):
        explicit = str(fields[_PHASE_FIELD])
    role = resolve_role(kind, explicit, location.template)
    return location, resolve_role_file(location, role, _file_stems(app, location))


@replayable_write
async def _runbook_capture_impl(
    app: DatacronApp,
    *,
    subject: str,
    kind: str,
    evidence: str,
    source: str,
    fields: dict[str, str],
    observed_on: str | None = None,
    target_role: str | None = None,
    dry_run: bool = False,
    actor: str = "direct-call",
    request_id: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        runbooks, writer = app.runbooks, app.runbook_writer
        if runbooks is None or writer is None:
            raise RunbookRoutingError(_REFUSAL_NOT_CONFIGURED)
        if not isinstance(fields, Mapping):
            raise RunbookEntryError("fields must be an object of strings")
        cleaned_source = _source(source)
        observed = _observed(observed_on)
        if not dry_run:
            replayed = await _journal_replay(app)
            if replayed is not None:
                return replayed
        location, target_file = _plan(
            app, runbooks, subject=subject, kind=kind, target_role=target_role, fields=fields
        )
        entry = render_entry(
            kind,
            fields,
            evidence=evidence,
            observed_on=observed,
            template=location.template,
            redactor=app.secret_redactor,
        )
        admit_runbook_write(
            target_file.rel_path,
            vault_root=app.vault_root,
            runbooks=runbooks,
            write_paths=app.settings.runbook_write_paths,
        )
        current = (
            _read_exact(app.vault_root / target_file.rel_path)
            if target_file.exists
            else _new_file(location.template, target_file.role)
        )
        _preview, action = _apply(
            current,
            entry,
            evidence=evidence,
            observed_on=observed,
            template=location.template,
            app=app,
        )
        if not target_file.exists and action != ACTION_SKIPPED:
            action = ACTION_CREATED
        plan: dict[str, Any] = {
            "runbook": location.directory,
            "rel_path": target_file.rel_path,
            "role": target_file.role,
            "heading": entry.heading,
            "action": action,
            "language": location.template.language,
            "runbook_created": not location.exists and action != ACTION_SKIPPED,
        }
        if dry_run or action == ACTION_SKIPPED:
            plan.update(dry_run=dry_run, written=False)
            _audit(RUNBOOK_TOOL_NAME, started, rel_path=target_file.rel_path, action=action)
            return plan
    except ReplayedWriteError:
        raise
    except (RunbookRoutingError, RunbookEntryError, RunbookScopeError, WriteConflictError) as exc:
        return _error_response(RUNBOOK_TOOL_NAME, exc, started)
    except Exception:
        return _internal_error_response(RUNBOOK_TOOL_NAME, started)
    return await _write(
        app,
        writer=writer,
        plan=plan,
        entry=entry,
        target_file=target_file,
        template=location.template,
        evidence=evidence,
        observed=observed,
        kind=kind,
        source=cleaned_source,
        actor=actor,
        started=started,
    )


async def _write(
    app: DatacronApp,
    *,
    writer: Any,
    plan: dict[str, Any],
    entry: RunbookEntry,
    target_file: RunbookFile,
    template: RunbookTemplate,
    evidence: str,
    observed: date,
    kind: str,
    source: str,
    actor: str,
    started: float,
) -> dict[str, Any]:
    rel_path = target_file.rel_path
    parameters: dict[str, str | int | float | bool | None] = {
        "kind": kind,
        "evidence": evidence,
        "role": target_file.role,
        "observed_on": observed.isoformat(),
        "source": source,
        "entry_chars": len(entry.body),
    }

    def mutation(raw: str) -> str:
        rendered, outcome = _apply(
            raw, entry, evidence=evidence, observed_on=observed, template=template, app=app
        )
        if outcome == ACTION_SKIPPED:
            raise _NothingToWriteError
        return rendered

    async def action() -> dict[str, Any]:
        app.write_policy.ensure_writable()
        try:
            if target_file.exists:
                parameters["action"] = plan["action"]
                content_hash = await writer.mutate_note_atomic(
                    rel_path,
                    mutation,
                    operation=OperationContext(
                        op=_OP_APPEND, tool=RUNBOOK_TOOL_NAME, actor=actor, parameters=parameters
                    ),
                )
            else:
                parameters["action"] = ACTION_CREATED
                content, _outcome = _apply(
                    _new_file(template, target_file.role),
                    entry,
                    evidence=evidence,
                    observed_on=observed,
                    template=template,
                    app=app,
                )
                content_hash = await writer.write_note_atomic(
                    rel_path,
                    content,
                    overwrite=False,
                    operation=OperationContext(
                        op=_OP_CREATE, tool=RUNBOOK_TOOL_NAME, actor=actor, parameters=parameters
                    ),
                )
        except _NothingToWriteError:
            plan.update(action=ACTION_SKIPPED, dry_run=False, written=False)
            return plan
        except PathConfinementError as exc:
            raise RunbookScopeError(
                f"runbook write refused: {rel_path!r} crosses a link or leaves the runbook "
                "write paths"
            ) from exc
        await _reconcile_committed_write(app, content_hash, rel_path)
        plan.update(
            dry_run=False,
            written=True,
            content_hash=content_hash,
            operation_id=await _latest_operation_id(app, rel_path, content_hash),
            indexed=True,
        )
        _audit(RUNBOOK_TOOL_NAME, started, rel_path=rel_path, action=plan["action"])
        return plan

    return await _execute_write_tool(
        RUNBOOK_TOOL_NAME,
        started,
        action,
        app=app,
        audit_fields={"rel_path": rel_path},
        expected=(
            DurabilityUnavailableError,
            ReadOnlyModeError,
            FileExistsError,
            FileNotFoundError,
            FrontmatterError,
            RunbookScopeError,
            RunbookEntryError,
            ValueError,
        ),
    )


def register_runbook_tools(
    server: MCPServer[Any], app: DatacronApp, *, annotations: ToolAnnotations
) -> None:
    """Register ``runbook_capture``; the caller decides whether the server offers it."""

    @server.tool(
        name=RUNBOOK_TOOL_NAME,
        title="Capture a runbook fact",
        description=RUNBOOK_CAPTURE_DESCRIPTION,
        annotations=annotations,
    )
    async def runbook_capture(
        subject: str,
        kind: str,
        evidence: str,
        source: str,
        fields: dict[str, str],
        ctx: Context[Any, Any],
        observed_on: str | None = None,
        target_role: str | None = None,
        dry_run: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await _runbook_capture_impl(
            app,
            subject=subject,
            kind=kind,
            evidence=evidence,
            source=source,
            fields=fields,
            observed_on=observed_on,
            target_role=target_role,
            dry_run=dry_run,
            actor=app.identity_provider.identify(ctx).actor,
            request_id=request_id,
        )
