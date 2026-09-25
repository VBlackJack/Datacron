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
"""Writes and the journal readers pass the note admission every read passes.

The scoped writer checked confinement alone. A note under an excluded folder, or
with an excluded name, was created, appended to and patched although no read would
ever serve it; a patch naming a wrong heading answered with that note's headings;
a junction inside the vault carried a write into an excluded folder; and the
journal readers returned the path and headings of excluded notes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import OperationRecord
from datacron.core.scope import (
    IO_REPARSE_TAG_MOUNT_POINT,
    ScopedVaultWriter,
    SingleTenantVaultScope,
)
from datacron.core.security import SecretRedactor
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools import (
    _append_journal_impl,
    _audit_query_impl,
    _create_note_ai_impl,
    _delete_note_section_impl,
    _get_note_history_impl,
    _get_note_impl,
    _move_note_section_impl,
    _patch_note_preamble_impl,
    _patch_note_section_impl,
    _rename_note_section_impl,
    _set_frontmatter_impl,
)
from datacron.mcp.tools.ops import _admitted_records, _operation_payload

_EXCLUDED_ID = "01J00000000000000000000A01"
_ADMITTED_ID = "01J00000000000000000000A02"
_EXCLUSIONS = "excluded_folders: [private]\nexcluded_files: [blocked.md]\n"
_EXCLUDED_BODY = "# P\n\n## Salary Bob 95k\n\nsecret salary data\n"
_HISTORY_LIMIT = 50

AppFactory = Callable[..., Awaitable[DatacronApp]]


def _note(root: Path, rel_path: str, note_id: str, body: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize({"id": note_id, "tags": []}, body), encoding="utf-8")
    return path


def _exclude(root: Path) -> None:
    config = root / ".datacron" / "VAULT.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(_EXCLUSIONS, encoding="utf-8")


def _create_directory_link(link: Path, target: Path) -> None:
    """Create a directory symlink, using an NTFS junction when needed."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory symlinks are unavailable: {exc}")
    command_shell = os.environ.get("COMSPEC")
    assert command_shell is not None, "COMSPEC is required to create an NTFS junction"
    process = subprocess.run(
        [command_shell, "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".datacron" not in path.relative_to(root).parts
    }


@pytest.fixture
async def open_app() -> AsyncIterator[AppFactory]:
    stores: list[SQLiteFTS5Store] = []

    async def factory(root: Path, write_root: Path | None = None) -> DatacronApp:
        settings = Settings(
            read_paths=[root],
            write_paths=[write_root or root],
            vault_root=root,
            redact_secrets="all",
        )
        store = SQLiteFTS5Store()
        await store.open(root / ".datacron" / "index" / "datacron.db")
        stores.append(store)
        return build_app(settings=settings, vault_root=root, chunker=MarkdownChunker(), store=store)

    try:
        yield factory
    finally:
        for store in stores:
            await store.close()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    _note(root, "Private/p.md", _EXCLUDED_ID, _EXCLUDED_BODY)
    _note(root, "blocked.md", "01J00000000000000000000A03", "# B\n\n## H\n\nblocked\n")
    _note(root, "notes/a.md", _ADMITTED_ID, "# A\n\n## H\n\nadmitted\n")
    _exclude(root)
    return root


@pytest.mark.parametrize("rel_path", ["Private/new.md", "private/new.md", "Private/sub/new.md"])
async def test_a_note_cannot_be_created_where_no_read_would_admit_it(
    vault: Path, open_app: AppFactory, rel_path: str
) -> None:
    app = await open_app(vault)
    before = _tree(vault)

    result = await _create_note_ai_impl(
        app,
        rel_path=rel_path,
        title="T",
        body="agent wrote this",
        origin="ai",
        confidence="high",
        tags=["x"],
    )

    assert "error" in result, result
    assert _tree(vault) == before


@pytest.mark.parametrize("rel_path", ["BLOCKED.md", "sub/Blocked.md"])
async def test_an_excluded_file_name_cannot_be_created_anywhere(
    vault: Path, open_app: AppFactory, rel_path: str
) -> None:
    app = await open_app(vault)
    before = _tree(vault)

    result = await _create_note_ai_impl(
        app,
        rel_path=rel_path,
        title="T",
        body="agent wrote this",
        origin="ai",
        confidence="high",
        tags=["x"],
    )

    assert result["error"]["code"] == "note_not_admitted", result
    assert _tree(vault) == before


async def test_every_writer_refuses_an_existing_excluded_note(
    vault: Path, open_app: AppFactory
) -> None:
    app = await open_app(vault)
    target = vault / "Private" / "p.md"
    before = target.read_bytes()
    current = sha256_bytes(before)
    calls: dict[str, Awaitable[dict[str, Any]]] = {
        "append_journal": _append_journal_impl(
            app, rel_path="Private/p.md", heading="Salary Bob 95k", entry="hi"
        ),
        "patch_note_section": _patch_note_section_impl(
            app, rel_path="Private/p.md", heading="Salary Bob 95k", new_content="x"
        ),
        "patch_note_preamble": _patch_note_preamble_impl(
            app, rel_path="Private/p.md", new_content="x", expected_hash=current
        ),
        "rename_note_section": _rename_note_section_impl(
            app, rel_path="Private/p.md", heading="Salary Bob 95k", new_heading="Other"
        ),
        "delete_note_section": _delete_note_section_impl(
            app, rel_path="Private/p.md", heading="Salary Bob 95k"
        ),
        "set_frontmatter": _set_frontmatter_impl(
            app, rel_path="Private/p.md", confidence="low", expected_hash=current
        ),
        "move_note_section": _move_note_section_impl(
            app,
            rel_path="Private/p.md",
            heading="Salary Bob 95k",
            destination_heading="P",
            expected_hash=current,
            confirm=True,
        ),
        "move_note_section preview": _move_note_section_impl(
            app,
            rel_path="Private/p.md",
            heading="Salary Bob 95k",
            destination_heading="P",
            expected_hash=current,
        ),
    }

    for tool, call in calls.items():
        result = await call
        assert result.get("error", {}).get("code") == "note_not_admitted", (tool, result)
        assert target.read_bytes() == before, tool


async def test_a_wrong_heading_on_an_excluded_note_suggests_nothing(
    vault: Path, open_app: AppFactory
) -> None:
    app = await open_app(vault)

    result = await _patch_note_section_impl(
        app, rel_path="Private/p.md", heading="Salary", new_content="x"
    )

    assert result["error"]["code"] == "note_not_admitted", result
    assert "suggestions" not in result["error"]
    assert "Bob" not in str(result)


async def test_the_refusal_does_not_tell_an_existing_excluded_note_from_a_missing_one(
    vault: Path, open_app: AppFactory
) -> None:
    app = await open_app(vault)

    existing = await _append_journal_impl(app, rel_path="Private/p.md", heading="H", entry="x")
    missing = await _append_journal_impl(app, rel_path="Private/q.md", heading="H", entry="x")

    assert existing["error"]["type"] == missing["error"]["type"]
    assert existing["error"]["code"] == missing["error"]["code"]
    assert existing["error"]["message"].replace("p.md", "q.md") == missing["error"]["message"]


async def test_a_directory_link_cannot_carry_a_write_into_an_excluded_folder(
    vault: Path, open_app: AppFactory
) -> None:
    _create_directory_link(vault / "link", vault / "Private")
    app = await open_app(vault)
    target = vault / "Private" / "p.md"
    before = target.read_bytes()

    created = await _create_note_ai_impl(
        app,
        rel_path="link/new.md",
        title="T",
        body="agent wrote this",
        origin="ai",
        confidence="high",
        tags=["x"],
    )
    deleted = await _delete_note_section_impl(app, rel_path="link/p.md", heading="Salary Bob 95k")

    assert created["error"]["type"] == "PathConfinementError", created
    assert "link" in created["error"]["message"]
    assert deleted["error"]["type"] == "PathConfinementError", deleted
    assert not (vault / "Private" / "new.md").exists()
    assert target.read_bytes() == before


async def test_a_directory_link_inside_the_vault_is_not_followed_by_a_write(
    vault: Path, open_app: AppFactory
) -> None:
    _create_directory_link(vault / "alias", vault / "notes")
    app = await open_app(vault)

    result = await _append_journal_impl(app, rel_path="alias/a.md", heading="H", entry="x")

    assert result["error"]["type"] == "PathConfinementError", result
    assert "link" in result["error"]["message"]
    assert str(vault) not in result["error"]["message"]


async def test_an_admitted_note_is_still_written(vault: Path, open_app: AppFactory) -> None:
    app = await open_app(vault)

    created = await _create_note_ai_impl(
        app,
        rel_path="notes/new.md",
        title="T",
        body="body",
        origin="ai",
        confidence="high",
        tags=["x"],
    )
    appended = await _append_journal_impl(app, rel_path="notes/a.md", heading="H", entry="more")

    assert "error" not in created, created
    assert "error" not in appended, appended
    assert "more" in (vault / "notes" / "a.md").read_text(encoding="utf-8")


async def test_a_refusal_outside_the_write_roots_names_no_host_path(
    vault: Path, open_app: AppFactory
) -> None:
    app = await open_app(vault, vault / "notes")

    result = await _create_note_ai_impl(
        app,
        rel_path="elsewhere/new.md",
        title="T",
        body="body",
        origin="ai",
        confidence="high",
        tags=["x"],
    )

    assert result["error"]["type"] == "PathConfinementError", result
    assert "elsewhere/new.md" in result["error"]["message"]
    assert str(vault) not in result["error"]["message"]
    assert str(vault.parent) not in result["error"]["message"]


async def test_the_journal_readers_withhold_records_of_excluded_notes(
    tmp_path: Path, open_app: AppFactory
) -> None:
    root = tmp_path / "vault"
    _note(root, "Private/x.md", _EXCLUDED_ID, "# X\n\n## Salary review Bob 95k\n\nx\n")
    _note(root, "notes/a.md", _ADMITTED_ID, "# A\n\n## H\n\nadmitted\n")
    before_exclusion = await open_app(root)
    written = await _append_journal_impl(
        before_exclusion, rel_path="Private/x.md", heading="Salary review Bob 95k", entry="hi"
    )
    assert "error" not in written, written
    kept = await _append_journal_impl(
        before_exclusion, rel_path="notes/a.md", heading="H", entry="kept"
    )
    assert "error" not in kept, kept
    _exclude(root)
    # A record outlives its note: a moved or deleted admitted note keeps its history.
    (root / "notes" / "a.md").unlink()
    app = await open_app(root)

    audit = await _audit_query_impl(
        app, start=None, end=None, tool=None, note=None, limit=_HISTORY_LIMIT
    )
    history = await _get_note_history_impl(app, note="Private/x.md", limit=_HISTORY_LIMIT)
    admitted_history = await _get_note_history_impl(app, note="notes/a.md", limit=_HISTORY_LIMIT)

    assert [operation["rel_path"] for operation in audit["operations"]] == ["notes/a.md"]
    assert "Salary" not in str(audit)
    assert history["operations"] == []
    assert history["total"] == 0
    assert admitted_history["total"] == 1


def test_every_string_parameter_of_a_journal_record_is_redacted() -> None:
    app: Any = type(
        "App",
        (),
        {"secret_redactor": SecretRedactor(), "settings": Settings(redact_secrets="all")},
    )()
    record = OperationRecord(
        operation_id="op",
        timestamp="2026-09-25T00:00:00+00:00",
        op="move",
        tool="apply_organization_manifest",
        note_id=None,
        rel_path="archive/db_password=hunter2.md",
        before_hash=None,
        after_hash="a" * 64,
        actor="me",
        parameters={
            "batch_member_kind": "move",
            "source_rel_path": "inbox/db_password=hunter2.md",
            "heading": "api_key=hunter2",
            "count": 3,
        },
        history_stored=False,
    )

    payload = _operation_payload(app, record)

    assert "hunter2" not in str(payload)
    parameters = payload["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["batch_member_kind"] == "move"
    assert parameters["count"] == 3


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS alternate data streams")
async def test_a_colon_cannot_open_an_alternate_data_stream(
    vault: Path, open_app: AppFactory
) -> None:
    host = vault / "notes" / "a.md"
    Path(f"{host}:evil.md").write_text(
        serialize({"id": "01J00000000000000000000A09"}, "# ADS\n\nads-secret\n"), encoding="utf-8"
    )
    Path(f"{vault / 'blocked.md'}:x.md").write_text(
        serialize({"id": "01J00000000000000000000A08"}, "# ADS\n\nblocked\n"), encoding="utf-8"
    )
    app = await open_app(vault)

    for id_or_path in ("notes/a.md:evil.md", "blocked.md:x.md"):
        result = await _get_note_impl(app, id_or_path=id_or_path, fmt="full")
        assert result.get("error", {}).get("code") == "note_not_admitted", (id_or_path, result)
        assert "ads-secret" not in str(result)


_CLOUD_REPARSE_TAG = 0x9000001A
_BATCH_MEMBER_PATHS = (
    ".datacron/VAULT.yaml",
    ".datacron/ulids.json",
    "_attachments/img.png",
    "Private/p.md",
    "notes/a.md",
)


def _record(rel_path: str) -> OperationRecord:
    return OperationRecord(
        operation_id=f"op-{rel_path}",
        timestamp="2026-09-25T00:00:00+00:00",
        op="move",
        tool="apply_organization_manifest",
        note_id=None,
        rel_path=rel_path,
        before_hash=None,
        after_hash="a" * 64,
        actor="me",
        parameters={},
        history_stored=False,
    )


async def test_recovery_state_keeps_every_member_an_organization_batch_journals(
    vault: Path,
) -> None:
    """A blocked sidecar or excluded-note member must stay visible to health and repair.

    Filtering recovery by note admission made these vanish: health reported healthy
    while the writer refused every write, and repair answered "not found in scope".
    """
    items = tuple(SimpleNamespace(rel_path=rel_path) for rel_path in _BATCH_MEMBER_PATHS)
    records = [_record(rel_path) for rel_path in _BATCH_MEMBER_PATHS]

    class _Delegate:
        recovery_blocked = items

        async def inspect_recovery(self) -> tuple[SimpleNamespace, ...]:
            return items

        async def list_operations(self) -> list[OperationRecord]:
            return records

    scope = SingleTenantVaultScope(vault, Settings(write_paths=[vault]))
    writer = ScopedVaultWriter(cast("Any", _Delegate()), scope, cast("Any", None))

    assert [item.rel_path for item in writer.recovery_blocked] == list(_BATCH_MEMBER_PATHS)
    inspected = await writer.inspect_recovery()
    assert [item.rel_path for item in inspected] == list(_BATCH_MEMBER_PATHS)
    listed = await writer.list_operations()
    assert [record.rel_path for record in listed] == list(_BATCH_MEMBER_PATHS)


async def test_the_journal_tools_withhold_excluded_notes_but_keep_other_members(
    vault: Path, open_app: AppFactory
) -> None:
    app = await open_app(vault)

    kept = _admitted_records(app, [_record(rel_path) for rel_path in _BATCH_MEMBER_PATHS])

    assert [record.rel_path for record in kept] == [
        ".datacron/VAULT.yaml",
        ".datacron/ulids.json",
        "_attachments/img.png",
        "notes/a.md",
    ]


def _fake_lstat(monkeypatch: pytest.MonkeyPatch, targets: set[Path], reparse_tag: int) -> None:
    real_lstat = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        result = real_lstat(path, *args, **kwargs)
        if Path(path) not in targets:
            return result
        extra = {"st_file_attributes": 0x0400, "st_reparse_tag": reparse_tag}
        return os.stat_result(tuple(result)[:10], extra)

    monkeypatch.setattr(os, "lstat", lstat)


async def test_a_cloud_placeholder_is_not_a_link(
    vault: Path, open_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OneDrive Files-On-Demand marks placeholders as reparse points with a cloud tag."""
    app = await open_app(vault)
    _fake_lstat(monkeypatch, {vault / "notes", vault / "notes" / "a.md"}, _CLOUD_REPARSE_TAG)

    result = await _append_journal_impl(app, rel_path="notes/a.md", heading="H", entry="more")

    assert "error" not in result, result
    assert "more" in (vault / "notes" / "a.md").read_text(encoding="utf-8")


async def test_a_junction_tag_is_refused_whatever_the_platform(
    vault: Path, open_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = await open_app(vault)
    before = (vault / "notes" / "a.md").read_bytes()
    _fake_lstat(monkeypatch, {vault / "notes"}, IO_REPARSE_TAG_MOUNT_POINT)

    result = await _append_journal_impl(app, rel_path="notes/a.md", heading="H", entry="more")

    assert result["error"]["type"] == "PathConfinementError", result
    assert (vault / "notes" / "a.md").read_bytes() == before
