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
"""Property proof for deterministic organization planning."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from datacron.core.config import OrganizationConfig, OrganizationRule, VaultConfig
from datacron.organization import planner as planner_module
from datacron.organization.report import render_json

pytestmark = pytest.mark.invariants

_NOTE_CASE = st.tuples(
    st.sampled_from(("facts", "decisions", "projects", "nested/alpha", "nested/beta")),
    st.sampled_from(("memory/fact", "memory/decision", "project/datacron")),
    st.sampled_from(("2026-08-29", "2026-08-28", None)),
    st.booleans(),
    st.booleans(),
)


def _config() -> VaultConfig:
    return VaultConfig(
        organization=OrganizationConfig(
            scope="_memory",
            rules=(
                OrganizationRule(
                    tag="memory/fact",
                    folder="_memory/facts",
                    naming="{date}-{slug}",
                    max_kb=1,
                ),
                OrganizationRule(
                    tag="memory/decision",
                    folder="_memory/decisions",
                    naming="{date}-{slug}",
                ),
            ),
        )
    )


def _write_case(
    root: Path,
    index: int,
    case: tuple[str, str, str | None, bool, bool],
) -> Path:
    folder, tag, created, malformed, oversized = case
    stem = f"2026-08-29-note-{index}" if index % 2 == 0 else f"undated-note-{index}"
    path = root / "_memory" / folder / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if malformed:
        path.write_bytes(b"\xff\xfe\x00")
        return path
    metadata: dict[str, object] = {"title": f"note {index}", "tags": [tag]}
    if created is not None:
        metadata["created"] = created
    header = yaml.safe_dump(metadata, sort_keys=False).strip()
    body = "x" * 2048 if oversized else "content"
    path.write_text(f"---\n{header}\n---\n\n{body}\n", encoding="utf-8")
    return path


@settings(max_examples=20, deadline=None)
@given(cases=st.lists(_NOTE_CASE, min_size=1, max_size=12))
def test_planning_is_byte_identical_for_opposite_discovery_orders(
    cases: list[tuple[str, str, str | None, bool, bool]],
) -> None:
    """Discovery order cannot affect paths, skipped notes, deviations or JSON."""
    with TemporaryDirectory(prefix="datacron-organization-property-") as temporary:
        root = Path(temporary).resolve()
        candidates = [_write_case(root, index, case) for index, case in enumerate(cases)]

        multi = root / "_memory" / "projects" / "undated-multi.md"
        multi.parent.mkdir(parents=True, exist_ok=True)
        multi.write_text(
            "---\ntitle: multi\ntags:\n  - memory/fact\n---\n\n" + "x" * 2048,
            encoding="utf-8",
        )
        second_multi = root / "_memory" / "decisions" / "undated-second.md"
        second_multi.parent.mkdir(parents=True, exist_ok=True)
        second_multi.write_text(
            "---\ntitle: second\ntags:\n  - memory/fact\n---\n\ncontent\n",
            encoding="utf-8",
        )
        unreadable_a = root / "_memory" / "facts" / "a-unreadable.md"
        unreadable_a.parent.mkdir(parents=True, exist_ok=True)
        unreadable_a.write_bytes(b"\xff")
        unreadable_z = root / "_memory" / "nested" / "z-unreadable.md"
        unreadable_z.parent.mkdir(parents=True, exist_ok=True)
        unreadable_z.write_bytes(b"\xfe")
        candidates.extend((multi, second_multi, unreadable_a, unreadable_z))
        config = _config()
        descending = sorted(candidates, key=lambda path: path.as_posix(), reverse=True)
        ascending = list(reversed(descending))

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(planner_module, "_discover_note_paths", lambda _context: descending)
            ordered_a = planner_module._iter_note_paths(root, config)
            plan_a = planner_module.plan_organization(root, config)
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(planner_module, "_discover_note_paths", lambda _context: ascending)
            ordered_b = planner_module._iter_note_paths(root, config)
            plan_b = planner_module.plan_organization(root, config)

        rel_a = [path.relative_to(root).as_posix() for path in ordered_a]
        rel_b = [path.relative_to(root).as_posix() for path in ordered_b]
        assert rel_a == sorted(rel_a)
        assert rel_b == rel_a
        assert render_json(plan_a) == render_json(plan_b)

        def preserve_discovery_order(
            discovered: Iterable[Path],
            _context: object,
        ) -> list[Path]:
            return list(discovered)

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(planner_module, "_discover_note_paths", lambda _context: descending)
            patcher.setattr(planner_module, "_authorize_note_paths", preserve_discovery_order)
            unsorted_plan_a = planner_module.plan_organization(root, config)
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(planner_module, "_discover_note_paths", lambda _context: ascending)
            patcher.setattr(planner_module, "_authorize_note_paths", preserve_discovery_order)
            unsorted_plan_b = planner_module.plan_organization(root, config)

        assert render_json(unsorted_plan_a) == render_json(unsorted_plan_b)
