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
"""Read-only organization planner."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from datacron.core.config import OrganizationConfig, OrganizationRule, VaultConfig
from datacron.organization.planner import (
    DeviationKind,
    OrganizationConfigurationError,
    plan_organization,
)
from datacron.organization.report import render_json, render_text

_UNSET = object()
_RULES = OrganizationConfig(
    scope="_memory",
    rules=(
        OrganizationRule(tag="memory/preference", folder="_memory/preferences", naming="{slug}"),
        OrganizationRule(tag="memory/contact", folder="_memory/people", naming="{slug}"),
        OrganizationRule(tag="memory/session", folder="_memory/sessions", naming="{date}-{slug}"),
        OrganizationRule(tag="memory/project", folder="_memory/projects", naming="{slug}"),
        OrganizationRule(
            tag="memory/fact", folder="_memory/facts", naming="{date}-{slug}", max_kb=1
        ),
        OrganizationRule(tag="memory/decision", folder="_memory/decisions", naming="{date}-{slug}"),
    ),
)


def _write(
    root: Path,
    rel_path: str,
    tags: list[str],
    body: str = "content\n",
    *,
    created: object = "2026-08-29",
    updated: object = _UNSET,
) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, object] = {"title": "note", "tags": tags}
    if created is not _UNSET:
        metadata["created"] = created
    if updated is not _UNSET:
        metadata["updated"] = updated
    rendered = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
    path.write_text(f"---\n{rendered}\n---\n\n{body}", encoding="utf-8")
    return path


def _config(
    *,
    rules: OrganizationConfig = _RULES,
    excluded_folders: list[str] | None = None,
    excluded_files: list[str] | None = None,
) -> VaultConfig:
    updates: dict[str, object] = {"organization": rules}
    if excluded_folders is not None:
        updates["excluded_folders"] = excluded_folders
    if excluded_files is not None:
        updates["excluded_files"] = excluded_files
    return VaultConfig.model_validate(updates)


def test_vault_without_organization_block_is_inert(tmp_path: Path) -> None:
    """The non-regression guarantee for every vault published before this lot."""
    _write(tmp_path, "_memory/facts/whatever.md", ["memory/fact"])

    plan = plan_organization(tmp_path, VaultConfig())

    assert plan.scope is None
    assert plan.scanned == 0
    assert plan.deviations == ()
    assert render_text(plan).startswith("No organization rules")


def test_note_in_the_declared_folder_with_the_exact_date_is_clean(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/facts/2026-08-29-fine.md", ["memory/fact"])

    plan = plan_organization(tmp_path, _config())

    assert plan.governed == 1
    assert plan.deviations == ()
    assert plan.has_deviations is False


def test_wrong_folder_is_reported_with_its_target(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/facts/2026-08-29-decided.md", ["memory/decision"])

    plan = plan_organization(tmp_path, _config())

    assert [item.kind for item in plan.deviations] == [DeviationKind.WRONG_FOLDER]
    assert plan.deviations[0].expected == "_memory/decisions"
    assert plan.deviations[0].tag == "memory/decision"


def test_naming_deviation_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/facts/undated.md", ["memory/fact"])

    plan = plan_organization(tmp_path, _config())

    assert [item.kind for item in plan.deviations] == [DeviationKind.NAMING]


def test_over_size_is_reported_against_max_kb(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/facts/2026-08-29-big.md", ["memory/fact"], body="x" * 4096)

    plan = plan_organization(tmp_path, _config())

    assert [item.kind for item in plan.deviations] == [DeviationKind.OVER_SIZE]


def test_note_without_a_matching_rule_is_out_of_scope_not_a_deviation(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/facts/orphan.md", ["project/datacron"])

    plan = plan_organization(tmp_path, _config())

    assert plan.unmatched == 1
    assert plan.governed == 0
    assert plan.deviations == ()


def test_priority_governs_a_multi_tagged_note(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "_memory/decisions/2026-08-29-both.md",
        ["memory/fact", "memory/decision"],
    )

    plan = plan_organization(tmp_path, _config())

    assert plan.governed == 1
    assert {item.tag for item in plan.deviations} == {"memory/fact"}
    assert {item.kind for item in plan.deviations} == {DeviationKind.WRONG_FOLDER}


def test_unreadable_frontmatter_is_skipped_without_failing_the_scan(tmp_path: Path) -> None:
    broken = tmp_path / "_memory" / "facts" / "2026-08-29-broken.md"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("---\ntags: [unclosed\n---\nbody\n", encoding="utf-8")
    _write(tmp_path, "_memory/facts/2026-08-29-fine.md", ["memory/fact"])

    plan = plan_organization(tmp_path, _config())

    assert len(plan.skipped) == 1
    assert plan.skipped[0].rel_path.endswith("2026-08-29-broken.md")
    assert plan.governed == 1


def test_invalid_yaml_timestamp_is_skipped_without_escaping(tmp_path: Path) -> None:
    broken = tmp_path / "_memory" / "facts" / "invalid-date.md"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text(
        "---\ntitle: broken\ncreated: 2026-02-30\ntags: [memory/fact]\n---\nbody\n",
        encoding="utf-8",
    )

    plan = plan_organization(tmp_path, _config())

    assert plan.scanned == 1
    assert plan.governed == 0
    assert len(plan.skipped) == 1


def test_invalid_utf8_is_skipped_after_admission(tmp_path: Path) -> None:
    broken = tmp_path / "_memory" / "facts" / "broken.md"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"\xff\xfe")

    plan = plan_organization(tmp_path, _config())

    assert plan.scanned == 1
    assert plan.skipped[0].reason == "UnicodeDecodeError"


def test_only_the_scope_is_scanned(tmp_path: Path) -> None:
    _write(tmp_path, "outside/2026-08-29-outside.md", ["memory/fact"])
    _write(tmp_path, "_memory/facts/2026-08-29-inside.md", ["memory/fact"])

    plan = plan_organization(tmp_path, _config())

    assert plan.scanned == 1
    assert plan.scope == "_memory"


def test_canonical_exclusions_are_case_insensitive(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/NODE_MODULES/hidden.md", ["memory/fact"])
    _write(tmp_path, "_memory/ARCHIVE/hidden.md", ["memory/fact"])
    _write(tmp_path, "_memory/.hidden/hidden.md", ["memory/fact"])
    _write(tmp_path, "_memory/facts/SKIP.MD", ["memory/fact"])
    _write(tmp_path, "_memory/facts/2026-08-29-visible.MD", ["memory/fact"])

    plan = plan_organization(
        tmp_path,
        _config(excluded_folders=["archive"], excluded_files=["skip.md"]),
    )

    assert plan.scanned == 1
    assert plan.governed == 1


def test_directory_exclusions_do_not_hide_similarly_named_files(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/projects/.note.md", ["memory/project"])
    _write(tmp_path, "_memory/projects/archive.md", ["memory/project"])

    plan = plan_organization(
        tmp_path,
        _config(excluded_folders=["archive.md"]),
    )

    assert plan.scanned == 2
    assert plan.governed == 2
    assert plan.deviations == ()


def test_report_paths_remain_vault_relative(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/facts/2026-08-29-misplaced.md", ["memory/decision"])

    payload = json.loads(render_json(plan_organization(tmp_path, _config())))

    assert payload["scope"] == "_memory"
    assert payload["deviations"][0]["rel_path"].startswith("_memory/")


def test_active_empty_scope_reports_clean_not_missing_rules(tmp_path: Path) -> None:
    (tmp_path / "_memory").mkdir()

    plan = plan_organization(tmp_path, _config())

    assert plan.scanned == 0
    assert "No deviation found" in render_text(plan)


def test_deviations_are_sorted_by_path_then_kind(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/facts/zz.md", ["memory/decision"])
    _write(tmp_path, "_memory/facts/aa.md", ["memory/decision"])

    plan = plan_organization(tmp_path, _config())

    keys = [item.sort_key for item in plan.deviations]
    assert keys == sorted(keys)


def test_a_single_note_can_carry_several_deviations(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/projects/undated.md", ["memory/fact"], body="x" * 4096)

    plan = plan_organization(tmp_path, _config())

    assert {item.kind for item in plan.deviations} == {
        DeviationKind.WRONG_FOLDER,
        DeviationKind.NAMING,
        DeviationKind.OVER_SIZE,
    }


@pytest.mark.parametrize(
    ("created", "updated", "expected_deviation"),
    [
        ("2026-08-29", _UNSET, False),
        ("2026-08-28", _UNSET, True),
        ("2026-08-29T23:30:00-02:00", _UNSET, False),
        ("not-a-date", "2026-08-29", False),
        (_UNSET, _UNSET, True),
    ],
)
def test_date_template_uses_created_then_updated_without_timezone_conversion(
    tmp_path: Path,
    created: object,
    updated: object,
    expected_deviation: bool,
) -> None:
    _write(
        tmp_path,
        "_memory/facts/2026-08-29-note.md",
        ["memory/fact"],
        created=created,
        updated=updated,
    )

    plan = plan_organization(tmp_path, _config())

    assert (DeviationKind.NAMING in {item.kind for item in plan.deviations}) is expected_deviation


def test_date_matching_never_uses_file_mtime(tmp_path: Path) -> None:
    note = _write(
        tmp_path,
        "_memory/facts/2026-08-29-note.md",
        ["memory/fact"],
        created=_UNSET,
    )
    os.utime(note, (1_577_836_800, 1_577_836_800))

    plan = plan_organization(tmp_path, _config())

    assert {item.kind for item in plan.deviations} == {DeviationKind.NAMING}


def test_iso_date_template_is_independent_from_lifecycle_fields(tmp_path: Path) -> None:
    rules = OrganizationConfig(
        scope="_memory",
        rules=(
            OrganizationRule(
                tag="memory/fact",
                folder="_memory/facts",
                naming="{iso_date}-{slug}",
            ),
        ),
    )
    _write(
        tmp_path,
        "_memory/facts/2026-01-15-transcript.md",
        ["memory/fact"],
        created="2026-08-27T23:30:00-02:00",
        updated="2026-08-28",
    )

    assert plan_organization(tmp_path, _config(rules=rules)).deviations == ()


def test_template_without_date_ignores_lifecycle_fields(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "_memory/projects/datacron.md",
        ["memory/project"],
        created="not-a-date",
        updated=None,
    )

    assert plan_organization(tmp_path, _config()).deviations == ()


def test_absent_target_directory_under_scope_is_valid(tmp_path: Path) -> None:
    (tmp_path / "_memory").mkdir()
    rules = OrganizationConfig(
        scope="_memory",
        rules=(OrganizationRule(tag="memory/fact", folder="_memory/not-created"),),
    )

    plan = plan_organization(tmp_path, _config(rules=rules))

    assert plan.deviations == ()


@pytest.mark.parametrize("kind", ["missing", "scope-file", "target-file", "outside-target"])
def test_invalid_scope_or_target_is_rejected(tmp_path: Path, kind: str) -> None:
    folder = "_memory/facts"
    if kind != "missing":
        (tmp_path / "_memory").mkdir()
    if kind == "scope-file":
        (tmp_path / "_memory").rmdir()
        (tmp_path / "_memory").write_text("not a directory", encoding="utf-8")
    elif kind == "target-file":
        (tmp_path / "_memory" / "facts").write_text("not a directory", encoding="utf-8")
    elif kind == "outside-target":
        folder = "other/facts"
    rules = OrganizationConfig(
        scope="_memory",
        rules=(OrganizationRule(tag="memory/fact", folder=folder),),
    )

    with pytest.raises(OrganizationConfigurationError):
        plan_organization(tmp_path, _config(rules=rules))


def test_planner_does_not_reload_the_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, "_memory/facts/2026-08-29-note.md", ["memory/fact"])

    def fail_reload(_path: Path) -> VaultConfig:
        raise AssertionError("the planner must use the already loaded VaultConfig")

    monkeypatch.setattr("datacron.core.scope.load_vault_config", fail_reload)

    assert plan_organization(tmp_path, _config()).governed == 1


def test_planner_writes_nothing_to_the_vault(tmp_path: Path) -> None:
    """The whole lot is read-only; prove it rather than assert it in prose."""
    _write(tmp_path, "_memory/facts/undated.md", ["memory/fact"])
    before = {path: path.stat().st_mtime_ns for path in sorted(tmp_path.rglob("*"))}

    plan_organization(tmp_path, _config())

    after = {path: path.stat().st_mtime_ns for path in sorted(tmp_path.rglob("*"))}
    assert before == after
