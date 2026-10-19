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
"""Companion memory inbox: listing, review marks, diff and revert."""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from datacron.bootstrap import initialize_vault
from datacron.companion import review_state as review_state_module
from datacron.companion.api import companion_i18n_bundle
from datacron.companion.inbox import (
    InboxError,
    get_companion_inbox_diff,
    list_companion_inbox,
    review_companion_inbox,
)
from datacron.companion.review_state import (
    EMPTY_STATE_HASH,
    read_review_state,
    review_state_path,
    update_review_state,
)
from datacron.companion.server import TOKEN_HEADER_NAME, create_companion_server
from datacron.companion.ui import render_companion_html
from datacron.config_contract import write_companion_profile_cas, write_vault_config_cas
from datacron.core.config import (
    COMPANION_REVERT_ACTOR,
    INBOX_DIFF_CONTEXT_LINES,
    INBOX_DIFF_FROM_LABEL,
    INBOX_DIFF_TO_LABEL,
    MEMORY_SUBFOLDER_NAME,
    SIDECAR_DIR_NAME,
    Settings,
    reset_settings_cache,
)
from datacron.core.hashing import sha256_bytes
from datacron.core.markdown_headings import split_markdown_lines
from datacron.core.operation_log import OperationContext, OperationJournal, OperationRecord
from datacron.core.paths import sidecar_index_db
from datacron.core.security import REDACTED
from datacron.mcp.server import build_app
from datacron.mcp.tools import (
    _append_journal_impl,
    _create_note_ai_impl,
    _patch_note_section_impl,
    _revert_note_impl,
)
from datacron.migration import migrate_vault_to_dual
from datacron.setup_wizard import default_write_paths

NOTE_REL = "_memory/facts/inbox-note.md"
NOTE_BODY = "# Inbox note\n\n## Journal\n\nfirst line\n"
PEM_BLOCK = (
    "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
    "KgwggSmAgEAAoIBAQC7VJTUt9Us8cKj\n-----END PRIVATE KEY-----"
)


@dataclass(frozen=True)
class Seeded:
    """Hashes and journal-visible tools of the four seeded writes."""

    created_hash: str
    appended_hash: str
    patched_hash: str
    reverted_hash: str


def _settings(vault: Path, memory: Path | None = None) -> Settings:
    read_paths = [vault] if memory is None else [vault, memory]
    return Settings(
        vault_root=vault,
        memory_root=memory,
        read_paths=read_paths,
        write_paths=default_write_paths(vault, memory_root=memory),
    )


def _profile(
    tmp_path: Path,
    vault: Path,
    *,
    memory: Path | None = None,
    enable_write: bool = True,
) -> Path:
    path = tmp_path / "companion.json"
    values: dict[str, Any] = {
        "vault_path": str(vault),
        "memory_vault_path": str(memory) if memory is not None else "",
        "enable_write": enable_write,
        "ui_language": "en",
    }
    write_companion_profile_cas(values, path=path, expect_absent=True)
    return path


async def _seed_four(vault: Path, memory: Path | None = None) -> Seeded:
    """Create, append, patch a section, then revert: four journaled writes on one note."""
    settings = _settings(vault, memory)
    app = build_app(settings=settings, vault_root=vault)
    await app.store.open(sidecar_index_db(vault, settings=settings))
    try:
        created = await _create_note_ai_impl(
            app,
            rel_path=NOTE_REL,
            title="Inbox note",
            body=NOTE_BODY,
            origin="ai",
            confidence="high",
            tags=["memory"],
            actor="agent-a",
        )
        appended = await _append_journal_impl(
            app,
            rel_path=NOTE_REL,
            heading="Journal",
            entry="second line",
            expected_hash=created["content_hash"],
            actor="agent-b",
        )
        patched = await _patch_note_section_impl(
            app,
            rel_path=NOTE_REL,
            heading="Journal",
            new_content="rewritten",
            expected_hash=appended["content_hash"],
            actor="agent-c",
        )
        reverted = await _revert_note_impl(
            app,
            note=NOTE_REL,
            to_hash=appended["content_hash"],
            expected_hash=patched["content_hash"],
            actor="agent-d",
        )
        for step in (created, appended, patched, reverted):
            assert "error" not in step, step
    finally:
        await app.store.close()
    return Seeded(
        created_hash=created["content_hash"],
        appended_hash=appended["content_hash"],
        patched_hash=patched["content_hash"],
        reverted_hash=reverted["content_hash"],
    )


async def _patch_section(vault: Path, new_content: str, actor: str = "agent-x") -> str:
    settings = _settings(vault)
    app = build_app(settings=settings, vault_root=vault)
    await app.store.open(sidecar_index_db(vault, settings=settings))
    try:
        current = (vault / NOTE_REL).read_bytes()
        result = await _patch_note_section_impl(
            app,
            rel_path=NOTE_REL,
            heading="Journal",
            new_content=new_content,
            expected_hash=sha256_bytes(current),
            actor=actor,
        )
        assert "error" not in result, result
        return str(result["content_hash"])
    finally:
        await app.store.close()


async def _replace_text(vault: Path, old: str, new: str) -> None:
    """Journal a write that swaps ``old`` for ``new`` and touches nothing else."""
    app = build_app(settings=_settings(vault), vault_root=vault)
    current = (vault / NOTE_REL).read_bytes()
    await app.vault_writer.mutate_note_atomic(
        NOTE_REL,
        lambda raw: raw.replace(old, new),
        expected_hash=sha256_bytes(current),
        operation=OperationContext(op="edit", tool="manual_edit", actor="agent-x", parameters={}),
    )


def _vault(tmp_path: Path, name: str = "vault") -> Path:
    vault = tmp_path / name
    initialize_vault(vault)
    return vault


def _list(profile: Path, **query: Any) -> dict[str, Any]:
    return asyncio.run(list_companion_inbox(query, profile_path=profile))


def _ids_by_tool(profile: Path) -> dict[str, str]:
    rows = _list(profile, unreviewed="false")["operations"]
    return {row["tool"]: row["operation_id"] for row in rows}


@contextmanager
def _serve(profile: Path) -> Iterator[tuple[str, str]]:
    server, base_url, token = create_companion_server(port=0, profile_path=profile)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield base_url, token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _http(
    base_url: str,
    path: str,
    *,
    token: str | None,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    origin: str | None = None,
) -> tuple[int, dict[str, Any]]:
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    headers: dict[str, str] = {}
    if token is not None:
        headers[TOKEN_HEADER_NAME] = token
    if origin is not None:
        headers["Origin"] = origin
    data: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# --- listing, review marks, restart ------------------------------------------------


def test_inbox_lists_the_four_writes_newest_first(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)

    result = _list(profile)

    rows = result["operations"]
    assert [row["tool"] for row in rows] == [
        "revert_note",
        "patch_note_section",
        "append_journal",
        "create_note_ai",
    ]
    assert [row["actor"] for row in rows] == ["agent-d", "agent-c", "agent-b", "agent-a"]
    assert result["total"] == 4
    assert result["writes_enabled"] is True
    assert all(row["rel_path"] == NOTE_REL for row in rows)
    assert all(row["title"] == "Inbox note" for row in rows)
    current_hash = sha256_bytes((vault / NOTE_REL).read_bytes())
    assert all(row["content_hash"] == current_hash for row in rows)
    can_revert = {row["tool"]: row["can_revert"] for row in rows}
    # A creation has no earlier text; the patch's earlier text is what the note holds now.
    assert can_revert == {
        "revert_note": True,
        "patch_note_section": False,
        "append_journal": True,
        "create_note_ai": False,
    }


def test_inbox_filters_and_pagination_are_bounded(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)

    assert _list(profile, tool="append_journal")["total"] == 1
    assert _list(profile, actor="agent-c")["operations"][0]["tool"] == "patch_note_section"
    assert _list(profile, note="INBOX-NOTE")["total"] == 4
    assert _list(profile, note="elsewhere")["total"] == 0
    assert _list(profile, since="24h")["total"] == 4
    page = _list(profile, limit="2", offset="1")
    assert [row["tool"] for row in page["operations"]] == ["patch_note_section", "append_journal"]
    assert page["total"] == 4
    assert page["limit_applied"] == 2
    for bad in ({"since": "yesterday"}, {"limit": "0"}, {"limit": "5000"}, {"offset": "-1"}):
        with pytest.raises((ValueError, InboxError)):
            _list(profile, **bad)
    with pytest.raises(InboxError):
        _list(profile, unreviewed="maybe")


def test_review_mark_hides_the_row_and_survives_a_restart(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)

    with _serve(profile) as (base_url, token):
        status, listing = _http(base_url, "/api/inbox", token=token)
        assert status == HTTPStatus.OK
        assert listing["state_hash"] == EMPTY_STATE_HASH
        target = listing["operations"][0]["operation_id"]
        status, marked = _http(
            base_url,
            "/api/inbox/review",
            token=token,
            method="POST",
            body={
                "operation_id": target,
                "reviewed": True,
                "expected_state_hash": listing["state_hash"],
            },
        )
        assert status == HTTPStatus.OK
        assert marked["reviewed"] is True
        _, after = _http(base_url, "/api/inbox", token=token)
        assert target not in [row["operation_id"] for row in after["operations"]]
        assert after["total"] == 3

    with _serve(profile) as (base_url, token):
        _, restarted = _http(base_url, "/api/inbox", token=token)
        assert restarted["total"] == 3
        _, everything = _http(base_url, "/api/inbox", token=token, query={"unreviewed": "false"})
        assert everything["total"] == 4
        reviewed = {row["operation_id"]: row["reviewed"] for row in everything["operations"]}
        assert reviewed[target] is True
        assert list(reviewed.values()).count(True) == 1
        _, unmarked = _http(
            base_url,
            "/api/inbox/review",
            token=token,
            method="POST",
            body={"operation_id": target, "reviewed": False},
        )
        assert unmarked["reviewed"] is False
        _, back = _http(base_url, "/api/inbox", token=token)
        assert back["total"] == 4


def test_concurrent_review_marks_from_threads_all_survive(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)
    operation_ids = list(_ids_by_tool(profile).values())
    barrier = threading.Barrier(len(operation_ids))
    failures: list[BaseException] = []

    def mark(operation_id: str) -> None:
        try:
            barrier.wait(timeout=10)
            asyncio.run(
                review_companion_inbox(
                    {"operation_id": operation_id, "reviewed": True}, profile_path=profile
                )
            )
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=mark, args=(op_id,)) for op_id in operation_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == []
    state = read_review_state(vault, _settings(vault))
    assert set(state.reviewed) == set(operation_ids)
    assert _list(profile)["total"] == 0


def test_stale_expected_state_hash_is_refused_over_http(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)
    ids = list(_ids_by_tool(profile).values())

    with _serve(profile) as (base_url, token):
        _, listing = _http(base_url, "/api/inbox", token=token)
        stale = listing["state_hash"]
        first = _http(
            base_url,
            "/api/inbox/review",
            token=token,
            method="POST",
            body={"operation_id": ids[0], "reviewed": True, "expected_state_hash": stale},
        )
        second = _http(
            base_url,
            "/api/inbox/review",
            token=token,
            method="POST",
            body={"operation_id": ids[1], "reviewed": True, "expected_state_hash": stale},
        )
    assert first[0] == HTTPStatus.OK
    assert second[0] == HTTPStatus.CONFLICT
    assert second[1]["status"] == "conflict"
    assert second[1]["state_hash"] == first[1]["state_hash"]
    assert set(read_review_state(vault, _settings(vault)).reviewed) == {ids[0]}


def test_review_state_prunes_unknown_operations_and_holds_its_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    settings = _settings(vault)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    known = {"op-1", "op-2", "op-3", "op-4", "op-5"}
    monkeypatch.setattr(review_state_module, "INBOX_REVIEW_MAX_ENTRIES", 3)

    for index, operation_id in enumerate(sorted(known), start=1):
        update_review_state(
            vault,
            settings,
            changes={operation_id: True},
            journal_ids=known,
            now=start + timedelta(minutes=index),
        )
    state = read_review_state(vault, settings)
    assert set(state.reviewed) == {"op-3", "op-4", "op-5"}

    pruned = update_review_state(
        vault,
        settings,
        changes={},
        journal_ids={"op-4"},
        now=start + timedelta(hours=1),
    )
    assert set(pruned.reviewed) == {"op-4"}
    assert read_review_state(vault, settings).state_hash == pruned.state_hash


def test_review_state_follows_the_journal_root_in_single_and_dual_vault(tmp_path: Path) -> None:
    vault = _vault(tmp_path, "docs")
    memory = _vault(tmp_path, "memory")
    single = review_state_path(vault, _settings(vault))
    dual = review_state_path(vault, _settings(vault, memory))

    assert single == vault.resolve() / SIDECAR_DIR_NAME / "companion" / "review-state.json"
    assert dual == memory.resolve() / SIDECAR_DIR_NAME / "companion" / "review-state.json"


def test_dual_vault_marks_live_beside_the_memory_journal(tmp_path: Path) -> None:
    docs = _vault(tmp_path, "docs")
    memory = _vault(tmp_path, "memory")
    asyncio.run(_seed_four(docs, memory))
    profile = _profile(tmp_path, docs, memory=memory)

    rows = _list(profile)["operations"]
    assert len(rows) == 4
    asyncio.run(
        review_companion_inbox(
            {"operation_id": rows[0]["operation_id"], "reviewed": True}, profile_path=profile
        )
    )

    marks = memory / SIDECAR_DIR_NAME / "companion" / "review-state.json"
    assert marks.is_file()
    assert not (docs / SIDECAR_DIR_NAME / "companion").exists()
    assert _list(profile)["total"] == 3


def test_cleanup_only_migration_succeeds_after_a_review_mark(tmp_path: Path) -> None:
    docs = _vault(tmp_path, "docs")
    (docs / MEMORY_SUBFOLDER_NAME).mkdir(exist_ok=True)
    (docs / MEMORY_SUBFOLDER_NAME / "fact.md").write_text("# Fact\n\nv1\n", encoding="utf-8")
    memory = tmp_path / "memory"
    asyncio.run(migrate_vault_to_dual(docs, memory, keep_source=True))

    # A mark made against the source sidecar after the copy has no counterpart in the
    # memory vault; like the journal, it must not block the cleanup of the source.
    late_mark = review_state_path(docs, _settings(docs))
    late_mark.parent.mkdir(parents=True)
    late_mark.write_text(
        '{"reviewed": {"op-9": {"reviewed_at": "2026-10-01T00:00:00+00:00"}}, "version": 1}\n',
        encoding="utf-8",
    )

    cleaned = asyncio.run(migrate_vault_to_dual(docs, memory, cleanup_only=True))

    assert cleaned.source_cleaned is True
    assert not (docs / MEMORY_SUBFOLDER_NAME).exists()


# --- diff --------------------------------------------------------------------------


def _read_blob(vault: Path, content_hash: str) -> bytes:
    return OperationJournal(vault, retention_days=30, history_mode="full").read_history(
        content_hash
    )


def test_diff_lines_equal_unified_diff_over_the_two_history_payloads(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    seeded = asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)
    append_id = _ids_by_tool(profile)["append_journal"]

    result = asyncio.run(
        get_companion_inbox_diff({"operation_id": append_id}, profile_path=profile)
    )

    before = _read_blob(vault, seeded.created_hash).decode("utf-8")
    after = _read_blob(vault, seeded.appended_hash).decode("utf-8")
    expected = list(
        difflib.unified_diff(
            [line.rstrip("\r\n") for line in split_markdown_lines(before)],
            [line.rstrip("\r\n") for line in split_markdown_lines(after)],
            fromfile=INBOX_DIFF_FROM_LABEL,
            tofile=INBOX_DIFF_TO_LABEL,
            n=INBOX_DIFF_CONTEXT_LINES,
            lineterm="",
        )
    )
    assert result["status"] == "diff"
    assert result["diff"] == "\n".join(expected)
    assert result["added"] == sum(1 for line in expected[2:] if line.startswith("+"))
    assert result["removed"] == sum(1 for line in expected[2:] if line.startswith("-"))
    assert result["added"] >= 1
    assert result["redacted_change_only"] is False
    assert result["truncated"] is False


def test_diff_of_a_creation_has_the_created_shape(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    seeded = asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)
    create_id = _ids_by_tool(profile)["create_note_ai"]

    result = asyncio.run(
        get_companion_inbox_diff({"operation_id": create_id}, profile_path=profile)
    )

    assert result["status"] == "created"
    assert result["content"] == _read_blob(vault, seeded.created_hash).decode("utf-8")


def test_diff_with_missing_history_is_unavailable_with_http_200(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    seeded = asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)
    append_id = _ids_by_tool(profile)["append_journal"]
    (vault / SIDECAR_DIR_NAME / "history" / seeded.created_hash).unlink()

    with _serve(profile) as (base_url, token):
        status, body = _http(
            base_url, "/api/inbox/diff", token=token, query={"operation_id": append_id}
        )
        _, listing = _http(base_url, "/api/inbox", token=token, query={"unreviewed": "false"})

    assert status == HTTPStatus.OK
    assert body == {"status": "unavailable"}
    row = next(item for item in listing["operations"] if item["operation_id"] == append_id)
    assert row["reviewed"] is False
    assert row["can_revert"] is False


def test_diff_replaces_a_multiline_private_key_with_the_marker(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    asyncio.run(_patch_section(vault, f"key follows\n\n{PEM_BLOCK}\n"))
    profile = _profile(tmp_path, vault)
    patch_id = _list(profile)["operations"][0]["operation_id"]

    result = asyncio.run(get_companion_inbox_diff({"operation_id": patch_id}, profile_path=profile))

    assert result["status"] == "diff"
    assert "MIIEvQIBADANBg" not in result["diff"]
    assert "KgwggSmAgEAAo" not in result["diff"]
    assert REDACTED in result["diff"]
    assert "+key follows" in result["diff"]


def test_diff_honours_a_custom_redaction_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATACRON_SECRET_REDACTION_PATTERNS", json.dumps(["ACME-[0-9]{6}"]))
    reset_settings_cache()
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    asyncio.run(_patch_section(vault, "ticket ACME-123456 opened\n"))
    profile = _profile(tmp_path, vault)
    patch_id = _list(profile)["operations"][0]["operation_id"]

    result = asyncio.run(get_companion_inbox_diff({"operation_id": patch_id}, profile_path=profile))

    assert "ACME-123456" not in result["diff"]
    assert f"ticket {REDACTED} opened" in result["diff"]


def test_a_secret_only_change_sets_redacted_change_only(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    first = "ghp_" + "A" * 36
    second = "ghp_" + "B" * 36
    asyncio.run(_patch_section(vault, f"token {first} here\n"))
    asyncio.run(_replace_text(vault, first, second))
    profile = _profile(tmp_path, vault)
    latest = _list(profile)["operations"][0]["operation_id"]

    result = asyncio.run(get_companion_inbox_diff({"operation_id": latest}, profile_path=profile))

    assert result["status"] == "diff"
    assert result["redacted_change_only"] is True
    assert result["added"] == 0
    assert result["removed"] == 0
    assert first not in json.dumps(result)
    assert second not in json.dumps(result)


# --- revert ------------------------------------------------------------------------


def _revert_body(listing: dict[str, Any], tool: str) -> dict[str, Any]:
    row = next(item for item in listing["operations"] if item["tool"] == tool)
    return {"operation_id": row["operation_id"], "expected_hash": row["content_hash"]}


def test_revert_restores_the_previous_bytes_and_is_journaled(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    seeded = asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)

    with _serve(profile) as (base_url, token):
        _, listing = _http(base_url, "/api/inbox", token=token, query={"unreviewed": "false"})
        status, result = _http(
            base_url,
            "/api/inbox/revert",
            token=token,
            method="POST",
            body=_revert_body(listing, "append_journal"),
        )
        _, after = _http(base_url, "/api/inbox", token=token, query={"unreviewed": "false"})

    assert status == HTTPStatus.OK
    assert result["indexed"] is True
    assert result["replayed"] is False
    assert result["content_hash"] == seeded.created_hash
    assert sha256_bytes((vault / NOTE_REL).read_bytes()) == seeded.created_hash
    newest = after["operations"][0]
    assert newest["tool"] == "revert_note"
    assert newest["actor"] == COMPANION_REVERT_ACTOR
    assert after["total"] == 5


def test_revert_with_a_stale_expected_hash_is_a_conflict_that_writes_nothing(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)
    before_bytes = (vault / NOTE_REL).read_bytes()

    with _serve(profile) as (base_url, token):
        _, listing = _http(base_url, "/api/inbox", token=token, query={"unreviewed": "false"})
        body = _revert_body(listing, "append_journal")
        body["expected_hash"] = "0" * 64
        status, result = _http(base_url, "/api/inbox/revert", token=token, method="POST", body=body)

    assert status == HTTPStatus.CONFLICT
    assert result["status"] == "conflict"
    assert (vault / NOTE_REL).read_bytes() == before_bytes
    assert _list(profile, unreviewed="false")["total"] == 4


def test_revert_is_refused_when_writes_are_disabled_by_the_profile(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault, enable_write=False)
    before_bytes = (vault / NOTE_REL).read_bytes()

    listing = _list(profile, unreviewed="false")
    assert listing["writes_enabled"] is False
    assert not any(row["can_revert"] for row in listing["operations"])
    with _serve(profile) as (base_url, token):
        status, result = _http(
            base_url,
            "/api/inbox/revert",
            token=token,
            method="POST",
            body=_revert_body(listing, "append_journal"),
        )
        marked, _ = _http(
            base_url,
            "/api/inbox/review",
            token=token,
            method="POST",
            body={"operation_id": listing["operations"][0]["operation_id"], "reviewed": True},
        )

    assert status == HTTPStatus.FORBIDDEN
    assert "error" in result
    assert (vault / NOTE_REL).read_bytes() == before_bytes
    # Marking a change as reviewed is not a note mutation, so it works without writes.
    assert marked == HTTPStatus.OK


def test_revert_is_refused_in_certified_read_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)
    monkeypatch.setenv("DATACRON_READ_ONLY", "true")
    reset_settings_cache()
    before_bytes = (vault / NOTE_REL).read_bytes()

    listing = _list(profile, unreviewed="false")
    assert listing["writes_enabled"] is False
    assert not any(row["can_revert"] for row in listing["operations"])
    with _serve(profile) as (base_url, token):
        status, _ = _http(
            base_url,
            "/api/inbox/revert",
            token=token,
            method="POST",
            body=_revert_body(listing, "append_journal"),
        )

    assert status == HTTPStatus.FORBIDDEN
    assert (vault / NOTE_REL).read_bytes() == before_bytes


def test_revert_double_submit_is_deduplicated(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)

    with _serve(profile) as (base_url, token):
        _, listing = _http(base_url, "/api/inbox", token=token, query={"unreviewed": "false"})
        body = _revert_body(listing, "append_journal")
        first = _http(base_url, "/api/inbox/revert", token=token, method="POST", body=body)
        second = _http(base_url, "/api/inbox/revert", token=token, method="POST", body=body)

    assert first[0] == HTTPStatus.OK
    assert first[1]["replayed"] is False
    assert second[0] == HTTPStatus.OK
    assert second[1]["replayed"] is True
    rows = _list(profile, unreviewed="false")["operations"]
    assert [row["actor"] for row in rows].count(COMPANION_REVERT_ACTOR) == 1


def test_revert_of_a_creation_or_of_purged_history_is_refused(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    seeded = asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)
    (vault / SIDECAR_DIR_NAME / "history" / seeded.created_hash).unlink()
    before_bytes = (vault / NOTE_REL).read_bytes()

    with _serve(profile) as (base_url, token):
        _, listing = _http(base_url, "/api/inbox", token=token, query={"unreviewed": "false"})
        creation = _http(
            base_url,
            "/api/inbox/revert",
            token=token,
            method="POST",
            body=_revert_body(listing, "create_note_ai"),
        )
        purged = _http(
            base_url,
            "/api/inbox/revert",
            token=token,
            method="POST",
            body=_revert_body(listing, "append_journal"),
        )

    assert creation[0] == HTTPStatus.CONFLICT
    assert purged[0] == HTTPStatus.CONFLICT
    assert (vault / NOTE_REL).read_bytes() == before_bytes


def test_an_operation_on_an_unadmitted_path_is_not_found(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    write_vault_config_cas(
        vault,
        {"excluded_folders": ["Private"], "archive_tags": [], "query_expansion": {}},
    )
    journal = OperationJournal(vault, retention_days=30, history_mode="full")
    journal.append_record(
        OperationRecord(
            operation_id="01J00000000000000000000001",
            timestamp="2026-10-17T10:00:00Z",
            op="create",
            tool="create_note_ai",
            note_id="01J00000000000000000000001",
            rel_path="Private/secret-plan.md",
            before_hash="a" * 64,
            after_hash="b" * 64,
            actor="test",
            parameters={},
            history_stored=False,
        )
    )
    profile = _profile(tmp_path, vault)
    private_op_id = "01J00000000000000000000001"

    with _serve(profile) as (base_url, token):
        _, listing = _http(base_url, "/api/inbox", token=token, query={"unreviewed": "false"})
        diff = _http(
            base_url, "/api/inbox/diff", token=token, query={"operation_id": private_op_id}
        )
        review = _http(
            base_url,
            "/api/inbox/review",
            token=token,
            method="POST",
            body={"operation_id": private_op_id, "reviewed": True},
        )
        revert = _http(
            base_url,
            "/api/inbox/revert",
            token=token,
            method="POST",
            body={"operation_id": private_op_id, "expected_hash": "b" * 64},
        )

    assert listing["operations"] == []
    assert diff[0] == HTTPStatus.NOT_FOUND
    assert review[0] == HTTPStatus.NOT_FOUND
    assert revert[0] == HTTPStatus.NOT_FOUND


def test_inbox_routes_refuse_a_missing_token_and_a_foreign_origin(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    asyncio.run(_seed_four(vault))
    profile = _profile(tmp_path, vault)
    target = next(iter(_ids_by_tool(profile).values()))
    body = {"operation_id": target, "reviewed": True}

    with _serve(profile) as (base_url, token):
        assert _http(base_url, "/api/inbox", token=None)[0] == HTTPStatus.FORBIDDEN
        assert _http(base_url, "/api/inbox/diff", token=None)[0] == HTTPStatus.FORBIDDEN
        for path in ("/api/inbox/review", "/api/inbox/revert"):
            no_token = _http(base_url, path, token=None, method="POST", body=body)
            assert no_token[0] == HTTPStatus.FORBIDDEN
            foreign = _http(
                base_url,
                path,
                token=token,
                method="POST",
                body=body,
                origin="http://evil.example",
            )
            assert foreign[0] == HTTPStatus.FORBIDDEN

    assert read_review_state(vault, _settings(vault)).reviewed == {}


# --- UI ----------------------------------------------------------------------------


def test_inbox_view_is_rendered_with_labelled_focusable_controls() -> None:
    html = render_companion_html("token", i18n=companion_i18n_bundle("en"), lang="en")

    assert 'data-tab="inbox"' in html
    assert 'id="tab-inbox"' in html
    section = html.split('id="tab-inbox"', 1)[1].split("</section>", 1)[0]
    controls = re.findall(r"<(?:button|input|select)\b[^>]*>", section)
    assert len(controls) >= 8
    for control in controls:
        assert "aria-label=" in control, control
    # Native controls are keyboard reachable; the scrollable diff region is focusable.
    assert re.search(r'id="inbox-diff"[^>]*tabindex="0"', section)
    # Rows are built by the page script with button semantics and a tab stop.
    assert 'item.setAttribute("role", "button")' in html
    assert 'item.setAttribute("tabindex", "0")' in html
    assert 'window.confirm(formatText(i18nText("inbox.revert_confirm")' in html


def test_every_inbox_label_exists_in_both_languages() -> None:
    html = render_companion_html("token", i18n=companion_i18n_bundle("en"), lang="en")
    english = companion_i18n_bundle("en")
    french = companion_i18n_bundle("fr")
    used = set(re.findall(r'data-i18n(?:-aria|-placeholder)?="(inbox\.[a-z_0-9]+)"', html))
    used |= set(re.findall(r'i18nText\("(inbox\.[a-z_0-9]+)"\)', html))
    used |= {f"inbox.{key}_aria" for key in ("mark_reviewed", "mark_unreviewed")}
    assert "inbox.revert_confirm" in used
    for dotted in used:
        section, key = dotted.split(".", 1)
        assert english[section][key].strip(), dotted
        assert french[section][key].strip(), dotted
    assert set(english["inbox"]) == set(french["inbox"])
    assert english["nav"]["inbox"] != french["nav"]["inbox"]


def test_inbox_diff_classes_use_theme_tokens_only() -> None:
    html = render_companion_html("token")
    without_root = re.sub(r":root\s*\{[^}]*\}", "", html, count=1)
    assert re.findall(r"#[0-9A-Fa-f]{6}\b", without_root) == []
    for css_class in ("diff-add", "diff-del", "diff-hunk"):
        assert re.search(rf"\.{css_class}\s*\{{[^}}]*var\(--", html)
