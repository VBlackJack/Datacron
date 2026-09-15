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
from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from datacron.core.config import OrganizationConfig, OrganizationRule, VaultConfig
from datacron.core.frontmatter import parse
from datacron.organization.planner import (
    DeviationKind,
    OrganizationConfigurationError,
    OrganizationPlan,
    plan_organization,
    snapshot_note,
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
    title: str = "note",
    aliases: list[str] | None = None,
    last_verified: object = _UNSET,
) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, object] = {"title": title, "tags": tags}
    if aliases is not None:
        metadata["aliases"] = aliases
    if created is not _UNSET:
        metadata["created"] = created
    if updated is not _UNSET:
        metadata["updated"] = updated
    if last_verified is not _UNSET:
        metadata["last_verified"] = last_verified
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


_SUBJECT_FOLDER = "_memory/subjects/heimdall"
_SUBJECT_TAGS = ["memory/fact", "project/heimdall"]
_STATE_TAGS = [*_SUBJECT_TAGS, "kind/platform"]
_STATE_EXPECTED = "one note carrying any kind/* tag"
_TODAY = date(2026, 9, 13)


def _subject_config(
    *,
    min_notes: int | None = None,
    since: str | None = None,
    with_policy: bool = True,
) -> VaultConfig:
    organization: dict[str, object] = {
        "scope": "_memory",
        "rules": [
            {"tag": "project/heimdall", "folder": _SUBJECT_FOLDER, "naming": "{slug}"},
            {"tag": "memory/fact", "folder": "_memory/facts", "naming": "{slug}"},
        ],
        "state_note_min_notes": min_notes,
        "linking_since": since,
    }
    if with_policy:
        organization["tags"] = {
            "placement_namespace": "memory",
            "subject_namespace": "project",
            "subjects": ["project/heimdall"],
            "allowed_namespaces": ["kind"],
        }
    return VaultConfig.model_validate({"organization": organization})


def _kinds(plan: OrganizationPlan) -> list[DeviationKind]:
    return [item.kind for item in plan.deviations]


def _write_state_note(
    root: Path,
    *,
    created: str = "2026-01-01",
    last_verified: object = _UNSET,
) -> Path:
    return _write(
        root,
        f"{_SUBJECT_FOLDER}/heimdall.md",
        _STATE_TAGS,
        title="Heimdall platform",
        aliases=["hd"],
        created=created,
        last_verified=last_verified,
    )


def test_no_state_note_is_reported_at_the_threshold(tmp_path: Path) -> None:
    for index in range(3):
        _write(tmp_path, f"{_SUBJECT_FOLDER}/note-{index}.md", _SUBJECT_TAGS)

    plan = plan_organization(tmp_path, _subject_config(min_notes=3))

    assert _kinds(plan) == [DeviationKind.NO_STATE_NOTE]
    deviation = plan.deviations[0]
    assert deviation.rel_path == _SUBJECT_FOLDER
    assert deviation.tag == "project/heimdall"
    assert deviation.detail == "3 notes, no kind/ tag"
    assert deviation.expected == _STATE_EXPECTED
    assert plan.scanned == plan.governed + plan.unmatched == 3


def test_no_state_note_is_silent_below_the_threshold(tmp_path: Path) -> None:
    for index in range(2):
        _write(tmp_path, f"{_SUBJECT_FOLDER}/note-{index}.md", _SUBJECT_TAGS)

    assert plan_organization(tmp_path, _subject_config(min_notes=3)).deviations == ()


def test_no_state_note_is_silent_when_a_kind_note_exists(tmp_path: Path) -> None:
    for index in range(2):
        _write(tmp_path, f"{_SUBJECT_FOLDER}/note-{index}.md", _SUBJECT_TAGS)
    _write_state_note(tmp_path)

    assert plan_organization(tmp_path, _subject_config(min_notes=3)).deviations == ()


def test_no_state_note_is_silent_without_the_key(tmp_path: Path) -> None:
    for index in range(3):
        _write(tmp_path, f"{_SUBJECT_FOLDER}/note-{index}.md", _SUBJECT_TAGS)

    assert plan_organization(tmp_path, _subject_config()).deviations == ()


def test_no_state_note_ignores_notes_outside_the_rule_folder(tmp_path: Path) -> None:
    for index in range(3):
        _write(tmp_path, f"_memory/facts/misplaced-{index}.md", _SUBJECT_TAGS)

    plan = plan_organization(tmp_path, _subject_config(min_notes=3))

    assert set(_kinds(plan)) == {DeviationKind.WRONG_FOLDER}


@pytest.mark.parametrize(
    ("argument", "value", "key"),
    [("min_notes", 3, "state_note_min_notes"), ("since", "2026-09-01", "linking_since")],
)
def test_folder_keys_without_a_subject_namespace_are_a_configuration_error(
    argument: str,
    value: object,
    key: str,
) -> None:
    """A silent no-op would hide a misconfiguration; the message names the key."""
    with pytest.raises(
        ValidationError, match=f"{key} requires organization.tags.subject_namespace"
    ):
        _subject_config(with_policy=False, **{argument: value})  # type: ignore[arg-type]


def test_unlinked_is_reported_for_a_dated_note_without_link(tmp_path: Path) -> None:
    _write_state_note(tmp_path)
    _write(
        tmp_path, f"{_SUBJECT_FOLDER}/2026-09-05-meeting.md", _SUBJECT_TAGS, created="2026-09-05"
    )

    plan = plan_organization(tmp_path, _subject_config(since="2026-09-01"))

    assert _kinds(plan) == [DeviationKind.UNLINKED]
    deviation = plan.deviations[0]
    assert deviation.rel_path == f"{_SUBJECT_FOLDER}/2026-09-05-meeting.md"
    assert deviation.tag == "project/heimdall"
    assert deviation.detail == "no wikilink to heimdall"
    assert deviation.expected == "[[heimdall]]"


def test_unlinked_is_silent_for_a_note_dated_before_linking_since(tmp_path: Path) -> None:
    _write_state_note(tmp_path)
    _write(tmp_path, f"{_SUBJECT_FOLDER}/older.md", _SUBJECT_TAGS, created="2026-08-31")

    assert plan_organization(tmp_path, _subject_config(since="2026-09-01")).deviations == ()


def test_unlinked_is_silent_for_the_state_note_itself(tmp_path: Path) -> None:
    _write_state_note(tmp_path, created="2026-09-10")

    assert plan_organization(tmp_path, _subject_config(since="2026-09-01")).deviations == ()


def test_unlinked_is_silent_for_a_split_history_note(tmp_path: Path) -> None:
    _write_state_note(tmp_path)
    _write(
        tmp_path,
        f"{_SUBJECT_FOLDER}/heimdall-history-2026-q1.md",
        _SUBJECT_TAGS,
        created="2026-09-10",
    )

    assert plan_organization(tmp_path, _subject_config(since="2026-09-01")).deviations == ()


@pytest.mark.parametrize(
    "link",
    ["[[heimdall]]", "[[HEIMDALL#Section|label]]", "[[Heimdall platform]]", "[[hd|the platform]]"],
    ids=["stem", "stem-anchor-label", "title", "alias"],
)
def test_unlinked_is_satisfied_by_stem_title_or_alias(tmp_path: Path, link: str) -> None:
    _write_state_note(tmp_path)
    _write(
        tmp_path,
        f"{_SUBJECT_FOLDER}/linked.md",
        _SUBJECT_TAGS,
        body=f"## Rattachements\n\n- {link}\n",
        created="2026-09-10",
    )

    assert plan_organization(tmp_path, _subject_config(since="2026-09-01")).deviations == ()


def test_a_wikilink_inside_a_fenced_block_does_not_count(tmp_path: Path) -> None:
    _write_state_note(tmp_path)
    _write(
        tmp_path,
        f"{_SUBJECT_FOLDER}/fenced.md",
        _SUBJECT_TAGS,
        body="```text\n[[heimdall]]\n```\n",
        created="2026-09-10",
    )

    plan = plan_organization(tmp_path, _subject_config(since="2026-09-01"))

    assert _kinds(plan) == [DeviationKind.UNLINKED]


def test_unlinked_is_silent_when_the_folder_has_no_state_note(tmp_path: Path) -> None:
    _write(tmp_path, f"{_SUBJECT_FOLDER}/alone.md", _SUBJECT_TAGS, created="2026-09-10")

    assert plan_organization(tmp_path, _subject_config(since="2026-09-01")).deviations == ()


def test_unlinked_is_silent_without_the_key(tmp_path: Path) -> None:
    _write_state_note(tmp_path)
    _write(tmp_path, f"{_SUBJECT_FOLDER}/unlinked.md", _SUBJECT_TAGS, created="2026-09-10")

    assert plan_organization(tmp_path, _subject_config()).deviations == ()


def test_any_state_note_of_the_folder_satisfies_the_link(tmp_path: Path) -> None:
    _write_state_note(tmp_path)
    _write(
        tmp_path,
        f"{_SUBJECT_FOLDER}/heimdall-mission.md",
        [*_SUBJECT_TAGS, "kind/mission"],
        created="2026-01-01",
    )
    _write(
        tmp_path,
        f"{_SUBJECT_FOLDER}/linked.md",
        _SUBJECT_TAGS,
        body="[[heimdall-mission]]\n",
        created="2026-09-10",
    )

    assert plan_organization(tmp_path, _subject_config(since="2026-09-01")).deviations == ()


def test_folder_deviation_sorts_with_the_notes_by_path(tmp_path: Path) -> None:
    for index in range(2):
        _write(tmp_path, f"{_SUBJECT_FOLDER}/note-{index}.md", _SUBJECT_TAGS, body="```\n")

    plan = plan_organization(tmp_path, _subject_config(min_notes=2))

    keys = [item.sort_key for item in plan.deviations]
    assert keys == sorted(keys)
    assert _kinds(plan) == [
        DeviationKind.NO_STATE_NOTE,
        DeviationKind.UNBALANCED_FENCE,
        DeviationKind.UNBALANCED_FENCE,
    ]


@pytest.mark.parametrize(
    ("fence_lines", "unbalanced"),
    [(0, False), (1, True), (2, False), (3, True), (4, False)],
)
def test_unbalanced_fence_is_reported_for_an_odd_fence_count(
    tmp_path: Path,
    fence_lines: int,
    unbalanced: bool,
) -> None:
    body = "".join(f"```\nline {index}\n" for index in range(fence_lines)) or "prose\n"
    _write(tmp_path, "_memory/facts/2026-08-29-fenced.md", ["memory/fact"], body=body)

    plan = plan_organization(tmp_path, _config())

    if unbalanced:
        assert _kinds(plan) == [DeviationKind.UNBALANCED_FENCE]
        assert plan.deviations[0].detail == f"{fence_lines} fence lines"
        assert plan.deviations[0].expected == "even number of fence lines"
    else:
        assert plan.deviations == ()


@pytest.mark.parametrize(
    ("indent", "counts"),
    [(0, True), (3, True), (4, False)],
    ids=["flush", "three-spaces", "four-spaces"],
)
def test_fence_indentation_up_to_three_spaces_counts(
    tmp_path: Path,
    indent: int,
    counts: bool,
) -> None:
    # A prose line first: the frontmatter parser strips the body's leading whitespace.
    body = f"prose\n{' ' * indent}```python\nx = 1\n"
    _write(tmp_path, "_memory/facts/2026-08-29-fenced.md", ["memory/fact"], body=body)

    plan = plan_organization(tmp_path, _config())

    assert (DeviationKind.UNBALANCED_FENCE in _kinds(plan)) is counts


def test_unbalanced_fence_needs_no_configuration_key(tmp_path: Path) -> None:
    _write(tmp_path, f"{_SUBJECT_FOLDER}/fenced.md", _SUBJECT_TAGS, body="```\n")

    plan = plan_organization(tmp_path, _subject_config())

    assert _kinds(plan) == [DeviationKind.UNBALANCED_FENCE]
    assert plan.deviations[0].tag == "project/heimdall"


def test_snapshot_derives_content_free_link_and_fence_facts() -> None:
    body = (
        "See [[Alpha#Intro|label]] and [[ beta ]] then [[alpha]] again.\n"
        "```\n[[hidden]]\n```\n"
        "~~~\n[[tilde-is-not-a-fence]]\n~~~\n"
        "[[#anchor-only]]\n"
    )

    snapshot = snapshot_note(
        "_memory/x.md",
        len(body),
        {"title": " Titled ", "aliases": ["one", "", "two"], "last_verified": "2026-09-01T10:00"},
        body,
    )

    # The canonical parser skips both fence kinds; only the backtick fence lines count.
    assert snapshot.wikilink_targets == ("alpha", "beta")
    assert snapshot.fence_lines == 2
    assert snapshot.fence_balanced is True
    assert snapshot.title == "Titled"
    assert snapshot.aliases == ("one", "two")
    assert snapshot.last_verified == "2026-09-01"
    assert snapshot.tags == ()


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        (date(2026, 9, 1), "2026-09-01"),
        ("2026-09-01", "2026-09-01"),
        ("not a date", None),
        (42, None),
        (None, None),
    ],
)
def test_snapshot_renders_last_verified_as_a_calendar_day(
    value: object,
    rendered: str | None,
) -> None:
    snapshot = snapshot_note("_memory/x.md", 0, {"last_verified": value}, "")

    assert snapshot.last_verified == rendered


@pytest.mark.parametrize(
    ("last_verified", "listed", "age_days"),
    [
        (_UNSET, True, None),
        ("2026-07-01", True, 74),
        ("2026-07-15", False, None),
        ("2026-09-10", False, None),
    ],
    ids=["missing", "older", "exactly-n-is-not-older", "newer"],
)
def test_freshness_lists_state_notes_older_than_the_threshold(
    tmp_path: Path,
    last_verified: object,
    listed: bool,
    age_days: int | None,
) -> None:
    """The boundary is strict: a note verified exactly N days ago is not listed."""
    _write_state_note(tmp_path, last_verified=last_verified)

    plan = plan_organization(tmp_path, _subject_config(), freshness_days=60, today=_TODAY)

    assert plan.freshness_days == 60
    assert plan.freshness is not None
    assert plan.has_deviations is False
    if listed:
        assert len(plan.freshness) == 1
        entry = plan.freshness[0]
        assert entry.rel_path == f"{_SUBJECT_FOLDER}/heimdall.md"
        assert entry.tag == "project/heimdall"
        assert entry.age_days == age_days
        assert entry.last_verified == (None if age_days is None else last_verified)
    else:
        assert plan.freshness == ()


def test_freshness_covers_every_governed_state_note_and_sorts_by_path(tmp_path: Path) -> None:
    _write_state_note(tmp_path)
    _write(tmp_path, "_memory/facts/zeta.md", ["memory/fact", "kind/mission"])
    _write(tmp_path, "_memory/facts/alpha.md", ["memory/fact", "kind/development"])
    _write(tmp_path, "_memory/facts/plain.md", ["memory/fact"])

    plan = plan_organization(tmp_path, _subject_config(), freshness_days=1, today=_TODAY)

    assert plan.freshness is not None
    assert [item.rel_path for item in plan.freshness] == [
        "_memory/facts/alpha.md",
        "_memory/facts/zeta.md",
        f"{_SUBJECT_FOLDER}/heimdall.md",
    ]


def test_freshness_is_absent_from_json_without_the_option(tmp_path: Path) -> None:
    _write_state_note(tmp_path)

    without = json.loads(render_json(plan_organization(tmp_path, _subject_config())))
    with_option = json.loads(
        render_json(plan_organization(tmp_path, _subject_config(), freshness_days=60, today=_TODAY))
    )

    assert "freshness" not in without
    assert without["schema"] == "organization-plan-v2"
    assert with_option["freshness"] == [
        {
            "age_days": None,
            "last_verified": None,
            "rel_path": f"{_SUBJECT_FOLDER}/heimdall.md",
            "tag": "project/heimdall",
        }
    ]


def test_freshness_text_block_follows_the_counts(tmp_path: Path) -> None:
    _write_state_note(tmp_path, last_verified="2026-07-01")

    text = render_text(
        plan_organization(tmp_path, _subject_config(), freshness_days=60, today=_TODAY)
    )

    assert "Freshness (older than 60 days): 1" in text
    assert f"  {_SUBJECT_FOLDER}/heimdall.md (project/heimdall, 74 days)" in text
    assert text.endswith("No deviation found.")


def test_text_report_lists_the_nine_counters(tmp_path: Path) -> None:
    (tmp_path / "_memory").mkdir()

    text = render_text(plan_organization(tmp_path, _config()))

    for kind in DeviationKind:
        assert f"  {kind.value:<16} 0" in text


@pytest.mark.parametrize(
    ("key", "value"),
    [("state_note_min_notes", 0), ("linking_since", "not-a-date")],
)
def test_invalid_folder_measurement_keys_are_rejected_at_load(key: str, value: object) -> None:
    with pytest.raises(ValidationError):
        OrganizationConfig.model_validate(
            {
                "scope": "_memory",
                "rules": [{"tag": "memory/fact", "folder": "_memory/facts"}],
                key: value,
            }
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("state_note_min_notes", True),
        ("state_note_min_notes", "5"),
        ("state_note_min_notes", 2.0),
        ("linking_since", 0),
        ("linking_since", 20260901),
        ("linking_since", True),
    ],
)
def test_folder_keys_are_read_strictly(key: str, value: object) -> None:
    """A boolean is not a count and a number is not a date."""
    organization: dict[str, object] = {
        "scope": "_memory",
        "rules": [
            {"tag": "project/heimdall", "folder": _SUBJECT_FOLDER},
            {"tag": "memory/fact", "folder": "_memory/facts"},
        ],
        "tags": {
            "placement_namespace": "memory",
            "subject_namespace": "project",
            "subjects": ["project/heimdall"],
        },
        key: value,
    }

    with pytest.raises(ValidationError, match=key):
        OrganizationConfig.model_validate(organization)


def test_linking_since_accepts_a_date_and_an_iso_string() -> None:
    as_date = _subject_config(since="2026-09-01").organization
    assert as_date is not None
    assert as_date.linking_since == date(2026, 9, 1)
    organization: dict[str, object] = {
        "scope": "_memory",
        "rules": [
            {"tag": "project/heimdall", "folder": _SUBJECT_FOLDER},
            {"tag": "memory/fact", "folder": "_memory/facts"},
        ],
        "tags": {
            "placement_namespace": "memory",
            "subject_namespace": "project",
            "subjects": ["project/heimdall"],
        },
        "linking_since": date(2026, 9, 2),
    }

    assert OrganizationConfig.model_validate(organization).linking_since == date(2026, 9, 2)


def test_a_numeric_alias_never_satisfies_a_link(tmp_path: Path) -> None:
    _write(
        tmp_path,
        f"{_SUBJECT_FOLDER}/heimdall.md",
        _STATE_TAGS,
        aliases=[123],  # type: ignore[list-item]
        created="2026-01-01",
    )
    _write(
        tmp_path,
        f"{_SUBJECT_FOLDER}/linked.md",
        _SUBJECT_TAGS,
        body="[[123]]\n",
        created="2026-09-10",
    )

    plan = plan_organization(tmp_path, _subject_config(since="2026-09-01"))

    assert _kinds(plan) == [DeviationKind.UNLINKED]


_CRLF_FRONTMATTER = (
    "---\r\ntitle: crlf\r\naliases:\r\n  - one\r\ntags:\r\n  - memory/fact\r\n---\r\n\r\n"
)
_CRLF_BODY = (
    "See [[Alpha]] and [[beta|label]].\r\n"
    "```\r\n#hidden [[fenced]]\r\n```\r\n"
    "#visible\r\n"
    "```\r\nopen fence\r\n"
)


@pytest.mark.parametrize("with_frontmatter", [True, False], ids=["frontmatter", "bare"])
def test_crlf_note_yields_the_same_snapshot_on_both_paths(
    tmp_path: Path,
    with_frontmatter: bool,
) -> None:
    """The scan reads universal newlines; the projection decodes raw bytes."""
    raw = ((_CRLF_FRONTMATTER if with_frontmatter else "") + _CRLF_BODY).encode("utf-8")
    note = tmp_path / "crlf.md"
    note.write_bytes(raw)

    scanned_metadata, scanned_body = parse(note.read_text(encoding="utf-8"))
    projected_metadata, projected_body = parse(raw.decode("utf-8"))
    scanned = snapshot_note("_memory/crlf.md", len(raw), scanned_metadata, scanned_body)
    projected = snapshot_note("_memory/crlf.md", len(raw), projected_metadata, projected_body)

    assert scanned == projected
    assert scanned.fence_lines == 3
    assert scanned.wikilink_targets == ("alpha", "beta")
    expected_tags = ("memory/fact", "visible") if with_frontmatter else ("visible",)
    assert scanned.tags == expected_tags
    if with_frontmatter:
        assert scanned.aliases == ("one",)


def test_vault_without_rules_still_answers_the_freshness_option(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/facts/whatever.md", ["memory/fact", "kind/platform"])

    plan = plan_organization(tmp_path, VaultConfig(), freshness_days=60, today=_TODAY)
    without = plan_organization(tmp_path, VaultConfig())

    assert plan.scope is None
    assert plan.freshness_days == 60
    assert plan.freshness == ()
    assert json.loads(render_json(plan))["freshness"] == []
    assert "freshness" not in json.loads(render_json(without))
    text = render_text(plan)
    assert text.startswith("No organization rules")
    assert text.endswith("Freshness (older than 60 days): 0")
