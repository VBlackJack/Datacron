# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.core.vault`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datacron.core.vault import FilesystemVaultReader, JsonIdStore


@pytest.mark.asyncio
class TestReadNote:
    async def test_with_frontmatter(self, vault_reader: FilesystemVaultReader) -> None:
        note = await vault_reader.read_note(vault_reader.vault_root / "welcome.md")
        assert note.title == "Welcome to the Demo Vault"
        assert "welcome" in note.tags
        assert "intro" in note.tags
        assert "Welcome" in note.aliases
        assert note.rel_path == "welcome.md"
        assert len(note.id) == 26
        assert note.content_hash != ""
        assert len(note.content_hash) == 64

    async def test_no_frontmatter_uses_h1(self, vault_reader: FilesystemVaultReader) -> None:
        note = await vault_reader.read_note(vault_reader.vault_root / "no-frontmatter.md")
        assert note.title == "No Frontmatter Here"
        assert note.frontmatter == {}
        assert "code" in note.tags

    async def test_empty_note(self, vault_reader: FilesystemVaultReader) -> None:
        note = await vault_reader.read_note(vault_reader.vault_root / "empty.md")
        assert note.title == "empty"
        assert note.content == ""
        assert note.frontmatter == {}

    async def test_subfolder_uses_posix_separator(
        self, vault_reader: FilesystemVaultReader
    ) -> None:
        note = await vault_reader.read_note(
            vault_reader.vault_root / "subfolder" / "nested-thoughts.md"
        )
        assert note.rel_path == "subfolder/nested-thoughts.md"

    async def test_important_flag_preserved(self, vault_reader: FilesystemVaultReader) -> None:
        note = await vault_reader.read_note(vault_reader.vault_root / "important-note.md")
        assert note.frontmatter.get("important") is True

    async def test_rejects_path_outside_vault(
        self,
        vault_reader: FilesystemVaultReader,
        tmp_path: Path,
    ) -> None:
        outside = tmp_path / "outside.md"
        outside.write_text("# outside", encoding="utf-8")
        with pytest.raises(ValueError, match="outside the vault root"):
            await vault_reader.read_note(outside)

    async def test_missing_file_raises(self, vault_reader: FilesystemVaultReader) -> None:
        with pytest.raises(FileNotFoundError):
            await vault_reader.read_note(vault_reader.vault_root / "does-not-exist.md")


@pytest.mark.asyncio
class TestListNotes:
    async def test_lists_all_markdown(self, vault_reader: FilesystemVaultReader) -> None:
        notes = await vault_reader.list_notes()
        rel_paths = {n.rel_path for n in notes}
        assert {
            "welcome.md",
            "no-frontmatter.md",
            "important-note.md",
            "empty.md",
            "code-snippets.md",
            "subfolder/nested-thoughts.md",
        } <= rel_paths

    async def test_skips_hidden_and_node_modules(
        self,
        vault_reader: FilesystemVaultReader,
    ) -> None:
        vault_root = vault_reader.vault_root
        hidden = vault_root / ".obsidian"
        hidden.mkdir()
        (hidden / "ignored.md").write_text("# nope", encoding="utf-8")
        node_modules = vault_root / "node_modules"
        node_modules.mkdir()
        (node_modules / "ignored.md").write_text("# nope", encoding="utf-8")

        notes = await vault_reader.list_notes()
        for note in notes:
            assert ".obsidian" not in note.rel_path
            assert "node_modules" not in note.rel_path

    async def test_skips_configured_excluded_folders(self, tmp_vault: Path) -> None:
        attachments = tmp_vault / "_attachments"
        attachments.mkdir()
        (attachments / "ignored.md").write_text("# ignored", encoding="utf-8")
        nested = tmp_vault / "nested" / "_attachments"
        nested.mkdir(parents=True)
        (nested / "also-ignored.md").write_text("# ignored", encoding="utf-8")
        kept = tmp_vault / "nested" / "kept.md"
        kept.write_text("# kept", encoding="utf-8")
        reader = FilesystemVaultReader(
            tmp_vault,
            excluded_folders=frozenset({"_attachments"}),
        )

        notes = await reader.list_notes()
        rel_paths = {note.rel_path for note in notes}

        assert "nested/kept.md" in rel_paths
        assert "_attachments/ignored.md" not in rel_paths
        assert "nested/_attachments/also-ignored.md" not in rel_paths

    async def test_folder_scope(self, vault_reader: FilesystemVaultReader) -> None:
        notes = await vault_reader.list_notes(folder="subfolder")
        assert {n.rel_path for n in notes} == {"subfolder/nested-thoughts.md"}

    async def test_limit_truncates(self, vault_reader: FilesystemVaultReader) -> None:
        notes = await vault_reader.list_notes(limit=2)
        assert len(notes) == 2

    async def test_folder_escape_rejected(self, vault_reader: FilesystemVaultReader) -> None:
        with pytest.raises(ValueError, match="escapes vault root"):
            await vault_reader.list_notes(folder="..")


@pytest.mark.asyncio
class TestResolveAlias:
    async def test_resolves_title(self, vault_reader: FilesystemVaultReader) -> None:
        target = await vault_reader.resolve_alias("Important Note")
        assert target is not None
        assert len(target) == 26

    async def test_resolves_alias_field(self, vault_reader: FilesystemVaultReader) -> None:
        target = await vault_reader.resolve_alias("Hello")
        assert target is not None

    async def test_resolves_filename_stem(self, vault_reader: FilesystemVaultReader) -> None:
        target = await vault_reader.resolve_alias("no-frontmatter")
        assert target is not None

    async def test_missing_alias_returns_none(self, vault_reader: FilesystemVaultReader) -> None:
        assert await vault_reader.resolve_alias("Phantom Note") is None

    async def test_empty_alias_returns_none(self, vault_reader: FilesystemVaultReader) -> None:
        assert await vault_reader.resolve_alias("   ") is None

    async def test_title_wins_over_alias_global_priority(self, tmp_path: Path) -> None:
        """Strict global priority per contracts §2.6: a title match on ANY note wins
        over an alias match on ANY other note, regardless of file iteration order.
        """
        # Note A: title "shared-key" (filename ordered AFTER B alphabetically)
        (tmp_path / "z-note.md").write_text(
            "---\ntitle: shared-key\n---\n# Body A\n", encoding="utf-8"
        )
        # Note B: alias "shared-key" (filename ordered BEFORE A → iterated first)
        (tmp_path / "a-note.md").write_text(
            "---\ntitle: Note B\naliases: [shared-key]\n---\n# Body B\n",
            encoding="utf-8",
        )
        reader = FilesystemVaultReader(tmp_path)
        target = await reader.resolve_alias("shared-key")
        assert target is not None
        # Resolves to Note A (title-tier match), not Note B (alias-tier match)
        note_a = await reader.read_note(tmp_path / "z-note.md")
        assert target == note_a.id

    async def test_ambiguous_titles_return_none(self, tmp_path: Path) -> None:
        """Two notes claiming the same title at tier 1 → unresolved, do not fall
        through to lower tiers."""
        (tmp_path / "one.md").write_text("---\ntitle: dup\n---\n# Body\n", encoding="utf-8")
        (tmp_path / "two.md").write_text("---\ntitle: dup\n---\n# Body\n", encoding="utf-8")
        reader = FilesystemVaultReader(tmp_path)
        assert await reader.resolve_alias("dup") is None


@pytest.mark.asyncio
class TestIdPersistence:
    async def test_id_is_stable_across_reads(self, vault_reader: FilesystemVaultReader) -> None:
        note_a = await vault_reader.read_note(vault_reader.vault_root / "welcome.md")
        note_b = await vault_reader.read_note(vault_reader.vault_root / "welcome.md")
        assert note_a.id == note_b.id

    async def test_id_persisted_to_sidecar(self, tmp_vault: Path) -> None:
        reader = FilesystemVaultReader(tmp_vault)
        note = await reader.read_note(tmp_vault / "welcome.md")
        sidecar = tmp_vault / ".datacron" / "ulids.json"
        assert sidecar.exists()
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert data["welcome.md"] == note.id

    async def test_frontmatter_id_honored(self, tmp_vault: Path) -> None:
        fixed_id = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
        target = tmp_vault / "welcome.md"
        new_content = target.read_text(encoding="utf-8").replace(
            "title: Welcome to the Demo Vault",
            f"title: Welcome to the Demo Vault\nid: {fixed_id}",
        )
        target.write_text(new_content, encoding="utf-8")

        reader = FilesystemVaultReader(tmp_vault)
        note = await reader.read_note(target)
        assert note.id == fixed_id

    async def test_id_store_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "ulids.json"
        store = JsonIdStore(path)
        assert await store.get("a.md") is None
        await store.set("a.md", "01HQXR7K9YZ8M2N3PQRSTV4WX5")
        snapshot = await store.snapshot()
        assert snapshot == {"a.md": "01HQXR7K9YZ8M2N3PQRSTV4WX5"}

        reloaded = JsonIdStore(path)
        assert await reloaded.get("a.md") == "01HQXR7K9YZ8M2N3PQRSTV4WX5"

    async def test_id_store_repairs_from_migrated_sidecar(self, tmp_path: Path) -> None:
        path = tmp_path / "ulids.json"
        migrated = tmp_path / "ulids.json.migrated"
        migrated.write_text(json.dumps({"a.md": "01HQXR7K9YZ8M2N3PQRSTV4WX5"}), encoding="utf-8")

        store = JsonIdStore(path)

        assert await store.get("a.md") == "01HQXR7K9YZ8M2N3PQRSTV4WX5"
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "a.md": "01HQXR7K9YZ8M2N3PQRSTV4WX5"
        }

    async def test_migrated_sidecar_wins_over_generated_duplicate(self, tmp_path: Path) -> None:
        path = tmp_path / "ulids.json"
        migrated = tmp_path / "ulids.json.migrated"
        path.write_text(json.dumps({"a.md": "01HQXR7K9YZ8M2N3PQRSTV4WX6"}), encoding="utf-8")
        migrated.write_text(json.dumps({"a.md": "01HQXR7K9YZ8M2N3PQRSTV4WX5"}), encoding="utf-8")

        store = JsonIdStore(path)

        assert await store.get("a.md") == "01HQXR7K9YZ8M2N3PQRSTV4WX5"
