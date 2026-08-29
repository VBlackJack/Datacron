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

from pathlib import Path

from datacron.core.config import OrganizationConfig, OrganizationRule, VaultConfig
from datacron.organization.planner import DeviationKind, plan_organization
from datacron.organization.report import render_json, render_text

_RULES = OrganizationConfig(
    rules=(
        OrganizationRule(tag="memory/decision", folder="_memory/decisions", naming="{date}-{slug}"),
        OrganizationRule(
            tag="memory/fact", folder="_memory/facts", naming="{date}-{slug}", max_kb=1
        ),
        OrganizationRule(tag="memory/project", folder="_memory/projects", naming="{slug}"),
    )
)


def _write(root: Path, rel_path: str, tags: list[str], body: str = "content\n") -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = "\n".join(f"  - {tag}" for tag in tags)
    path.write_text(f"---\ntitle: note\ntags:\n{rendered}\n---\n\n{body}", encoding="utf-8")
    return path


def _config() -> VaultConfig:
    return VaultConfig(organization=_RULES)


def test_vault_without_organization_block_is_inert(tmp_path: Path) -> None:
    """The non-regression guarantee for every vault published before this lot."""
    _write(tmp_path, "_memory/facts/whatever.md", ["memory/fact"])

    plan = plan_organization(tmp_path, VaultConfig())

    assert plan.scanned == 0
    assert plan.deviations == ()
    assert render_text(plan).startswith("No organization rules")


def test_note_in_the_declared_folder_is_clean(tmp_path: Path) -> None:
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


def test_first_matching_rule_governs_a_multi_tagged_note(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/facts/2026-08-29-both.md", ["memory/fact", "memory/decision"])

    plan = plan_organization(tmp_path, _config())

    assert [item.tag for item in plan.deviations] == ["memory/decision"]


def test_unreadable_frontmatter_is_skipped_without_failing_the_scan(tmp_path: Path) -> None:
    broken = tmp_path / "_memory" / "facts" / "2026-08-29-broken.md"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("---\ntags: [unclosed\n---\nbody\n", encoding="utf-8")
    _write(tmp_path, "_memory/facts/2026-08-29-fine.md", ["memory/fact"])

    plan = plan_organization(tmp_path, _config())

    assert len(plan.skipped) == 1
    assert plan.skipped[0].rel_path.endswith("2026-08-29-broken.md")
    assert plan.governed == 1


def test_excluded_and_dot_folders_are_not_scanned(tmp_path: Path) -> None:
    _write(tmp_path, "_archive/old/undated.md", ["memory/fact"])
    _write(tmp_path, ".datacron/history/undated.md", ["memory/fact"])
    _write(tmp_path, "_memory/facts/2026-08-29-fine.md", ["memory/fact"])

    plan = plan_organization(tmp_path, _config())

    assert plan.scanned == 1


def test_report_is_byte_identical_across_runs(tmp_path: Path) -> None:
    """Determinism is the contract that makes the report diffable and CI-usable."""
    for index in range(12):
        _write(tmp_path, f"_memory/facts/note-{index}.md", ["memory/decision"])

    first = render_json(plan_organization(tmp_path, _config()))
    second = render_json(plan_organization(tmp_path, _config()))

    assert first == second


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


def test_planner_writes_nothing_to_the_vault(tmp_path: Path) -> None:
    """The whole lot is read-only; prove it rather than assert it in prose."""
    _write(tmp_path, "_memory/facts/undated.md", ["memory/fact"])
    before = {path: path.stat().st_mtime_ns for path in sorted(tmp_path.rglob("*"))}

    plan_organization(tmp_path, _config())

    after = {path: path.stat().st_mtime_ns for path in sorted(tmp_path.rglob("*"))}
    assert before == after
