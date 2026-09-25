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
"""Negative controls for conversation evidence and operational guidance."""

from pathlib import Path

from datacron.eval.conversation_trace import ConversationCase, TraceEvent, grade
from datacron.mcp.guidance import health_guidance


def test_conversation_grade_rejects_unread_claims_and_unverified_writes() -> None:
    case = ConversationCase(
        name="resume",
        minimum_sessions=2,
        required_tools={"get_note": 1},
        cited_paths=["project.md"],
        required_final_text=["pending"],
        forbidden_final_text=["completed"],
    )
    events = [TraceEvent(session="one", answer="completed project.md")]
    failed = grade(case, events)
    assert not failed["passed"]
    assert "unverified_citation:project.md" in failed["failures"]
    assert "insufficient_sessions" in failed["failures"]
    events = [
        TraceEvent(
            session="one",
            tool="append_journal",
            arguments={"rel_path": "project.md"},
            result={"indexed": True, "content_hash": "hash"},
        ),
        TraceEvent(
            session="two",
            tool="get_note",
            result={"rel_path": "project.md", "content_hash": "hash", "content": "pending"},
        ),
        TraceEvent(session="two", answer="pending project.md"),
    ]
    assert grade(case, events)["passed"]
    events[1].result["content_hash"] = "other"
    assert "write_not_reread:0" in grade(case, events)["failures"]


def test_replay_needs_a_current_read_and_empty_trace_never_passes() -> None:
    case = ConversationCase(name="replay")
    assert not grade(case, [])["passed"]
    events = [
        TraceEvent(
            session="one",
            tool="append_journal",
            arguments={"rel_path": "project.md"},
            result={"replayed": True, "indexed": False, "content_hash": "historical"},
        ),
        TraceEvent(session="one", answer="Saved"),
    ]
    assert "write_not_reread:0" in grade(case, events)["failures"]
    events.insert(
        1,
        TraceEvent(
            session="one",
            tool="get_note",
            result={"rel_path": "project.md", "content_hash": "current", "content": "Saved"},
        ),
    )
    assert grade(case, events)["passed"]


def test_health_guidance_preserves_recovery_priority() -> None:
    payload = {
        "recovery": {"required": True},
        "index": {"consistent_with_vault": False},
        "integrity": {"id_mismatches": 2},
        "durability": {"effective_writes_enabled": False},
        "status": "degraded",
    }
    result = health_guidance(payload)
    assert result[0]["code"] == "recovery_required"
    assert result[0]["severity"] == "blocking"
    assert {item["code"] for item in result} == {
        "recovery_required",
        "index_stale",
        "identity_mismatch",
        "writes_disabled",
    }


def test_installer_harness_refuses_non_disposable_host(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    script = Path(__file__).parents[2] / "scripts" / "verify_windows_install.py"
    env = dict(os.environ)
    env.pop("DATACRON_DISPOSABLE_MACHINE", None)
    report = tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--allow-install",
            "--installer",
            "missing.exe",
            "--sha256",
            "0" * 64,
            "--report",
            str(report),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert "DATACRON_DISPOSABLE_MACHINE=1" in result.stderr
    assert not report.exists()


def test_sandbox_bundle_confines_shares_and_never_overwrites(tmp_path: Path) -> None:
    import subprocess
    import sys
    import xml.etree.ElementTree as ET

    script = Path(__file__).parents[2] / "scripts" / "prepare_windows_sandbox.py"
    installer, validator = tmp_path / "installer.exe", tmp_path / "validator.exe"
    installer.write_bytes(b"fixture-not-executable")
    validator.write_bytes(b"fixture-not-executable")
    output = tmp_path / "bundle"
    command = [
        sys.executable,
        str(script),
        "--installer",
        str(installer),
        "--validator",
        str(validator),
        "--output",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    config = ET.parse(output / "validate.wsb").getroot()  # noqa: S314 - locally generated fixture
    assert config.findtext("Networking") == "Disable"
    assert [node.findtext("ReadOnly") for node in config.findall("MappedFolders/MappedFolder")] == [
        "true",
        "false",
    ]
    assert (output / "input" / "Datacron-Setup.exe").read_bytes() == installer.read_bytes()
    repeated = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    assert repeated.returncode != 0
