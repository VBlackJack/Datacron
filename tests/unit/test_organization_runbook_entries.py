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
"""Rendering of runbook captures, provenance and secret refusal."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest

from datacron.core.config import RunbookTemplate
from datacron.core.security import SecretRedactor
from datacron.organization.runbook_entries import (
    RUNBOOK_FIELD_MAX_CHARS,
    RunbookEntryError,
    entry_key,
    render_entry,
    render_update,
)

_DAY = date(2026, 10, 29)
_LABELS = {
    "symptom": "Symptom",
    "cause": "Cause",
    "fix": "Fix",
    "not_captured": "Not captured",
    "machine": "Machine",
    "output": "Output",
    "decisions": "Decisions",
    "reason": "Reason",
    "discarded": "Discarded",
    "open_questions": "Open questions",
    "owner": "Owner",
    "update": "Update",
    "placeholder": "Placeholder",
    "meaning": "Meaning",
    "value": "On site",
}
_TEMPLATE = RunbookTemplate.model_validate(
    {
        "language": "en",
        "readme": "README",
        "observation_label": "On site",
        "roles": {"architecture": {"stem": "00-architecture", "match": "^00-architecture$"}},
        "labels": _LABELS,
    }
)
# Words that would betray who wrote an entry; none may appear in rendered output.
_TRACE: re.Pattern[str] = re.compile(r"(?i)\b(?:ai|assistant|claude|session|model|llm)\b")


def _render(kind: str, evidence: str = "measured", **fields: Any) -> Any:
    return render_entry(
        kind,
        fields,
        evidence=evidence,
        observed_on=_DAY,
        template=_TEMPLATE,
        redactor=SecretRedactor(),
    )


def test_an_error_fix_is_a_symptom_section() -> None:
    entry = _render(
        "error_fix",
        symptom="A client stops receiving mail",
        cause="Proxy bypass list lacks the endpoint name",
        fix="Add the name to the bypass list",
    )

    assert (entry.heading, entry.heading_level, entry.owns_heading) == (
        "Symptom: A client stops receiving mail",
        2,
        True,
    )
    assert entry.body == (
        "**Cause.** Proxy bypass list lacks the endpoint name\n\n"
        "**Fix.** Add the name to the bypass list\n\n"
        "measured 2026-10-29"
    )
    assert entry.key == "a client stops receiving mail"


def test_a_missing_cause_is_said_not_invented() -> None:
    entry = _render("error_fix", "said", symptom="x", fix="y")

    assert "**Cause.** Not captured" in entry.body
    assert entry.body.endswith("said 2026-10-29")


def test_a_measured_command_needs_its_real_output() -> None:
    with pytest.raises(RunbookEntryError, match="real output"):
        _render("command", machine="client", heading="Checks", command="hostname")

    entry = _render(
        "command",
        "measured",
        machine="the client, elevated",
        heading="Collecting client evidence",
        command="hostname",
        syntax="powershell",
        output="srv-a01",
    )
    assert entry.body == (
        "Machine: the client, elevated.\n\n```powershell\nhostname\n```\n\n"
        "Output:\n\n```text\nsrv-a01\n```\n\nmeasured 2026-10-29"
    )


def test_an_unverified_command_says_its_output_was_not_captured() -> None:
    entry = _render("command", "unverified", machine="m", heading="h", command="c")

    assert "Output:\n\nNot captured" in entry.body


def test_a_fence_outgrows_the_backticks_it_holds() -> None:
    entry = _render("command", "unverified", machine="m", heading="h", command="echo ```x```")

    assert "````\necho ```x```\n````" in entry.body


def test_a_variable_is_a_table_row_keyed_by_its_placeholder() -> None:
    entry = _render(
        "server",
        placeholder="<UPSTREAM>",
        meaning="Primary mail server | short name",
        value="srv-a01",
        heading="Servers",
    )

    assert entry.row == (
        "`<UPSTREAM>`",
        "Primary mail server \\| short name",
        "`srv-a01` (measured 2026-10-29)",
    )
    assert entry.key == "<upstream>"
    with pytest.raises(RunbookEntryError, match="UPPER_SNAKE"):
        _render("variable", placeholder="upstream", meaning="m", value="v", heading="h")


def test_contact_decision_fact_and_question_shapes() -> None:
    contact = _render(
        "contact", "said", name="Network team", role="firewall", channel="ticket", heading="Who"
    )
    decision = _render(
        "decision", "said", decision="Publish one endpoint\nform only", reason="Readers copy URLs"
    )
    fact = _render("fact", heading="Forest", fact="Schema version 88")
    question = _render("open_question", "unverified", question="Who owns the VIP?", owner="Network")

    assert contact.body == "- **Network team**, firewall: ticket. said 2026-10-29"
    assert decision.heading == "Decisions"
    assert decision.body == (
        "- said 2026-10-29: Publish one endpoint\n  form only\n  **Reason.** Readers copy URLs"
    )
    assert fact.body == "> **On site, 2026-10-29.** Schema version 88 (measured 2026-10-29)"
    assert question.body == "- Who owns the VIP? **Owner:** Network. unverified 2026-10-29"


@pytest.mark.parametrize(
    "value",
    ["password=Hunter2Hunter2", "Authorization: Bearer abcdefghijklmnopqrstuv", "ghp_" + "a" * 36],
)
def test_a_secret_is_refused_and_never_echoed(value: str) -> None:
    with pytest.raises(
        RunbookEntryError, match="field 'fix' holds what looks like a secret"
    ) as caught:
        _render("error_fix", symptom="Login fails", fix=f"Use {value}")

    assert value not in str(caught.value)


def test_a_custom_pattern_from_the_settings_is_honoured() -> None:
    with pytest.raises(RunbookEntryError, match="secret"):
        render_entry(
            "fact",
            {"heading": "h", "fact": "ticket INTERNAL-12345678"},
            evidence="said",
            observed_on=_DAY,
            template=_TEMPLATE,
            redactor=SecretRedactor([r"INTERNAL-[0-9]{8}"]),
        )


@pytest.mark.parametrize(
    ("kind", "fields", "fragment"),
    [
        ("error_fix", {"symptom": "x"}, "requires the fields ['fix']"),
        ("error_fix", {"symptom": "x", "fix": "y", "fixx": "z"}, "not used by kind"),
        ("error_fix", {"symptom": "a\nb", "fix": "y"}, "single line"),
        ("error_fix", {"symptom": "x", "fix": "## New section"}, "reads as a heading"),
        ("error_fix", {"symptom": "x", "fix": "text\n====="}, "reads as a heading"),
        ("error_fix", {"symptom": "x", "fix": "y" * (RUNBOOK_FIELD_MAX_CHARS + 1)}, "at most"),
        ("error_fix", {"symptom": "x", "fix": 3}, "must be a string"),
        (
            "command",
            {"machine": "m", "heading": "h", "command": "c", "syntax": "a b"},
            "fence language",
        ),
        ("rumour", {}, "kind must be one of"),
    ],
)
def test_malformed_captures_are_refused(kind: str, fields: dict[str, Any], fragment: str) -> None:
    with pytest.raises(RunbookEntryError, match=re.escape(fragment)):
        _render(kind, "unverified", **fields)


def test_unknown_evidence_and_missing_labels_are_refused() -> None:
    with pytest.raises(RunbookEntryError, match="evidence must be one of"):
        _render("fact", "rumour", heading="h", fact="f")
    bare = _TEMPLATE.model_copy(update={"labels": {}})
    with pytest.raises(RunbookEntryError, match="declares no label 'symptom'"):
        render_entry(
            "error_fix",
            {"symptom": "x", "fix": "y"},
            evidence="said",
            observed_on=_DAY,
            template=bare,
            redactor=SecretRedactor(),
        )


def test_an_update_keeps_the_entry_and_dates_the_addition() -> None:
    text = render_update(
        "Also needed on replicas",
        evidence="measured",
        observed_on=_DAY,
        template=_TEMPLATE,
        redactor=SecretRedactor(),
    )

    assert text == "**Update, 2026-10-29.** Also needed on replicas (measured 2026-10-29)"


def test_keys_ignore_case_spacing_and_trailing_punctuation() -> None:
    assert entry_key("A client  stops receiving mail.") == entry_key(
        "a client stops receiving mail"
    )
    assert entry_key("m", "hostname") != entry_key("m", "hostname -f")


def test_no_rendered_entry_names_who_wrote_it() -> None:
    bodies = [
        _render("error_fix", symptom="s", fix="f").body,
        _render("command", "unverified", machine="m", heading="h", command="c").body,
        _render("decision", "said", decision="d", reason="r").body,
        _render("open_question", "said", question="q", owner="o").body,
    ]

    assert not [body for body in bodies if _TRACE.search(body)]
