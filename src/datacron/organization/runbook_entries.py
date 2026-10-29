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
"""Render one runbook capture as Markdown, or refuse it.

Every word comes from the template's labels; the only fixed text is the
provenance line, ``<measured|said|unverified> YYYY-MM-DD``, a neutral format
arbitrated on 2026-10-29 that names no tool, assistant or session. A field that
holds a likely secret is refused, never masked: a masked command is a wrong
command, and a runbook is read in order to be typed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, final

from datacron.core.config import RUNBOOK_CAPTURE_KINDS, RunbookTemplate
from datacron.core.security import SecretRedactor

__all__ = [
    "RUNBOOK_EVIDENCE",
    "RunbookEntry",
    "RunbookEntryError",
    "entry_key",
    "provenance",
    "render_entry",
    "render_update",
]

RUNBOOK_EVIDENCE: Final[tuple[str, ...]] = ("measured", "said", "unverified")
RUNBOOK_EVIDENCE_MEASURED: Final[str] = "measured"
RUNBOOK_FIELD_MAX_CHARS: Final[int] = 4000
RUNBOOK_LINE_FIELD_MAX_CHARS: Final[int] = 300
_HEADING_LEVEL_SECTION: Final[int] = 2

# Fields per kind: (required single-line, required multi-line, optional single-line,
# optional multi-line). A field outside these is refused, so a misspelled field
# name is never dropped in silence.
_FIELDS: Final[dict[str, tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]]] = {
    "error_fix": (frozenset({"symptom"}), frozenset({"fix"}), frozenset(), frozenset({"cause"})),
    "command": (
        frozenset({"machine", "heading"}),
        frozenset({"command"}),
        frozenset({"syntax", "phase"}),
        frozenset({"output"}),
    ),
    "server": (
        frozenset({"placeholder", "meaning", "value", "heading"}),
        frozenset(),
        frozenset(),
        frozenset(),
    ),
    "variable": (
        frozenset({"placeholder", "meaning", "value", "heading"}),
        frozenset(),
        frozenset(),
        frozenset(),
    ),
    "contact": (
        frozenset({"name", "role", "channel", "heading"}),
        frozenset(),
        frozenset(),
        frozenset(),
    ),
    "decision": (
        frozenset(),
        frozenset({"decision", "reason"}),
        frozenset(),
        frozenset({"discarded"}),
    ),
    "fact": (frozenset({"heading"}), frozenset({"fact"}), frozenset(), frozenset()),
    "open_question": (frozenset({"owner"}), frozenset({"question"}), frozenset(), frozenset()),
}

# Labels each kind renders. They are the template's words, in its language.
_LABELS: Final[dict[str, tuple[str, ...]]] = {
    "error_fix": ("symptom", "cause", "fix", "not_captured"),
    "command": ("machine", "output", "not_captured"),
    "server": ("placeholder", "meaning", "value"),
    "variable": ("placeholder", "meaning", "value"),
    "contact": (),
    "decision": ("decisions", "reason", "discarded"),
    "fact": (),
    "open_question": ("open_questions", "owner"),
}

_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"^<[A-Z][A-Z0-9_]*>$")
_SYNTAX: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9+-]*$")
# A line that Markdown reads as a heading would split the entry into sections the
# section writers then address; inside a fence it is code and stays.
_HEADING_LINE: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}(?:#{1,6}(?:\s|$)|=+\s*$|-{2,}\s*$)")
_BACKTICK_RUN: Final[re.Pattern[str]] = re.compile(r"`+")
_FENCE_MIN: Final[int] = 3
_ITEM_INDENT: Final[str] = "  "
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")
_TRAILING_PUNCTUATION: Final[str] = ".:;,!? "


class RunbookEntryError(ValueError):
    """A capture whose fields cannot be rendered as given."""


@final
@dataclass(frozen=True, slots=True)
class RunbookEntry:
    """A rendered capture and where it goes inside its file.

    ``heading`` is the H2 that holds the entry. For a symptom the heading is the
    entry itself and ``body`` is the whole section; otherwise ``body`` is appended
    under an existing or new ``heading``. ``row`` is set for a table entry, keyed
    by its first cell. ``key`` identifies the entry for deduplication.
    """

    kind: str
    heading: str
    heading_level: int
    body: str
    key: str
    row: tuple[str, ...] | None = None
    header: tuple[str, ...] | None = None
    owns_heading: bool = False


def entry_key(*parts: str) -> str:
    """Return the deduplication key: NFC, casefolded, whitespace collapsed."""
    folded = []
    for part in parts:
        text = unicodedata.normalize("NFC", part).casefold()
        folded.append(_WHITESPACE.sub(" ", text).strip(_TRAILING_PUNCTUATION))
    return " | ".join(folded)


def provenance(evidence: str, observed_on: date) -> str:
    """Return the neutral provenance line of an entry."""
    return f"{evidence} {observed_on.isoformat()}"


def _label(template: RunbookTemplate, name: str) -> str:
    try:
        return template.labels[name]
    except KeyError:
        raise RunbookEntryError(f"the template declares no label {name!r}") from None


def _clean_fields(kind: str, fields: Mapping[str, object]) -> dict[str, str]:
    required_line, required_text, optional_line, optional_text = _FIELDS[kind]
    allowed = required_line | required_text | optional_line | optional_text
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise RunbookEntryError(
            f"fields {unknown} are not used by kind {kind!r}; it takes {sorted(allowed)}"
        )
    cleaned: dict[str, str] = {}
    for name, value in fields.items():
        if not isinstance(value, str):
            raise RunbookEntryError(f"field {name!r} must be a string")
        text = value.strip()
        if not text:
            continue
        single_line = name in required_line | optional_line
        limit = RUNBOOK_LINE_FIELD_MAX_CHARS if single_line else RUNBOOK_FIELD_MAX_CHARS
        if len(text) > limit:
            raise RunbookEntryError(f"field {name!r} must be at most {limit} characters")
        if single_line and ("\n" in text or "\r" in text):
            raise RunbookEntryError(f"field {name!r} must be a single line")
        cleaned[name] = text
    missing = sorted((required_line | required_text) - set(cleaned))
    if missing:
        raise RunbookEntryError(f"kind {kind!r} requires the fields {missing}")
    return cleaned


def _refuse_secrets(fields: Mapping[str, str], redactor: SecretRedactor) -> None:
    for name in sorted(fields):
        if redactor.redact_text(fields[name]) != fields[name]:
            raise RunbookEntryError(
                f"field {name!r} holds what looks like a secret; nothing was written. "
                "Capture the step without the value, or with a placeholder"
            )


def _prose(name: str, text: str) -> str:
    for line in text.splitlines():
        if _HEADING_LINE.match(line):
            raise RunbookEntryError(
                f"field {name!r} holds a line Markdown reads as a heading: {line.strip()!r}"
            )
    return text


def _item(name: str, text: str) -> str:
    """Return list-item prose whose continuation lines stay inside the item."""
    return _prose(name, text).replace("\n", "\n" + _ITEM_INDENT)


def _fence(text: str, info: str = "") -> str:
    longest = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    marker = "`" * max(_FENCE_MIN, longest + 1)
    return f"{marker}{info}\n{text}\n{marker}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


@final
@dataclass(frozen=True, slots=True)
class _Capture:
    kind: str
    values: dict[str, str]
    labels: dict[str, str]
    evidence: str
    observed_on: date
    template: RunbookTemplate

    @property
    def line(self) -> str:
        return provenance(self.evidence, self.observed_on)

    def entry(self, heading: str, body: str, key: str, **extra: Any) -> RunbookEntry:
        return RunbookEntry(
            kind=self.kind,
            heading=heading,
            heading_level=_HEADING_LEVEL_SECTION,
            body=body,
            key=key,
            **extra,
        )


def _error_fix(capture: _Capture) -> RunbookEntry:
    values, labels = capture.values, capture.labels
    cause = _prose("cause", values["cause"]) if "cause" in values else labels["not_captured"]
    body = "\n\n".join(
        (
            f"**{labels['cause']}.** {cause}",
            f"**{labels['fix']}.** {_prose('fix', values['fix'])}",
            capture.line,
        )
    )
    heading = f"{labels['symptom']}: {values['symptom']}"
    return capture.entry(heading, body, entry_key(values["symptom"]), owns_heading=True)


def _command(capture: _Capture) -> RunbookEntry:
    values, labels = capture.values, capture.labels
    if capture.evidence == RUNBOOK_EVIDENCE_MEASURED and "output" not in values:
        raise RunbookEntryError("a measured command requires its real output")
    syntax = values.get("syntax", "")
    if syntax and not _SYNTAX.match(syntax):
        raise RunbookEntryError(f"field 'syntax' must be a fence language name; got {syntax!r}")
    output = _fence(values["output"], "text") if "output" in values else labels["not_captured"]
    body = "\n\n".join(
        (
            f"{labels['machine']}: {values['machine']}.",
            _fence(values["command"], syntax),
            f"{labels['output']}:",
            output,
            capture.line,
        )
    )
    return capture.entry(values["heading"], body, entry_key(values["machine"], values["command"]))


def _table_row(capture: _Capture) -> RunbookEntry:
    values = capture.values
    placeholder = values["placeholder"]
    if not _PLACEHOLDER.match(placeholder):
        raise RunbookEntryError(
            f"field 'placeholder' must look like <UPPER_SNAKE>; got {placeholder!r}"
        )
    row = (
        f"`{placeholder}`",
        _cell(values["meaning"]),
        f"`{_cell(values['value'])}` ({capture.line})",
    )
    body = "| " + " | ".join(row) + " |"
    labels = capture.labels
    header = (labels["placeholder"], labels["meaning"], labels["value"])
    return capture.entry(values["heading"], body, entry_key(placeholder), row=row, header=header)


def _contact(capture: _Capture) -> RunbookEntry:
    values = capture.values
    body = f"- **{values['name']}**, {values['role']}: {values['channel']}. {capture.line}"
    return capture.entry(values["heading"], body, entry_key(values["name"], values["role"]))


def _decision(capture: _Capture) -> RunbookEntry:
    values, labels = capture.values, capture.labels
    parts = [
        f"- {capture.line}: {_item('decision', values['decision'])}",
        f"{_ITEM_INDENT}**{labels['reason']}.** {_item('reason', values['reason'])}",
    ]
    if "discarded" in values:
        discarded = _item("discarded", values["discarded"])
        parts.append(f"{_ITEM_INDENT}**{labels['discarded']}.** {discarded}")
    return capture.entry(labels["decisions"], "\n".join(parts), entry_key(values["decision"]))


def _fact(capture: _Capture) -> RunbookEntry:
    values = capture.values
    fact = _prose("fact", values["fact"]).replace("\n", "\n> ")
    label = capture.template.observation_label
    body = f"> **{label}, {capture.observed_on.isoformat()}.** {fact} ({capture.line})"
    return capture.entry(values["heading"], body, entry_key(values["fact"]))


def _open_question(capture: _Capture) -> RunbookEntry:
    values, labels = capture.values, capture.labels
    body = (
        f"- {_item('question', values['question'])} "
        f"**{labels['owner']}:** {values['owner']}. {capture.line}"
    )
    return capture.entry(labels["open_questions"], body, entry_key(values["question"]))


_RENDERERS: Final[dict[str, Callable[[_Capture], RunbookEntry]]] = {
    "error_fix": _error_fix,
    "command": _command,
    "server": _table_row,
    "variable": _table_row,
    "contact": _contact,
    "decision": _decision,
    "fact": _fact,
    "open_question": _open_question,
}


def render_entry(
    kind: str,
    fields: Mapping[str, object],
    *,
    evidence: str,
    observed_on: date,
    template: RunbookTemplate,
    redactor: SecretRedactor,
) -> RunbookEntry:
    """Render a capture of ``kind`` with the template's words, or refuse it."""
    if kind not in RUNBOOK_CAPTURE_KINDS:
        raise RunbookEntryError(f"kind must be one of {list(RUNBOOK_CAPTURE_KINDS)}")
    if evidence not in RUNBOOK_EVIDENCE:
        raise RunbookEntryError(f"evidence must be one of {list(RUNBOOK_EVIDENCE)}")
    values = _clean_fields(kind, fields)
    _refuse_secrets(values, redactor)
    capture = _Capture(
        kind=kind,
        values=values,
        labels={name: _label(template, name) for name in _LABELS[kind]},
        evidence=evidence,
        observed_on=observed_on,
        template=template,
    )
    return _RENDERERS[kind](capture)


def render_update(
    text: str,
    *,
    evidence: str,
    observed_on: date,
    template: RunbookTemplate,
    redactor: SecretRedactor,
) -> str:
    """Render the paragraph a known entry receives instead of a second copy."""
    if evidence not in RUNBOOK_EVIDENCE:
        raise RunbookEntryError(f"evidence must be one of {list(RUNBOOK_EVIDENCE)}")
    cleaned = text.strip()
    if not cleaned:
        raise RunbookEntryError("an update must not be empty")
    _refuse_secrets({"update": cleaned}, redactor)
    label = _label(template, "update")
    dated = provenance(evidence, observed_on)
    return f"**{label}, {observed_on.isoformat()}.** {_prose('update', cleaned)} ({dated})"
