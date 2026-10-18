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
"""Key-by-key frontmatter edits that keep every byte of the block they do not touch.

Re-dumping a whole frontmatter block through PyYAML is lossy on hand-written notes:
comments go, key order and flow style change, and YAML 1.1 turns values into other
values (``14:30`` into ``870``, ``1.10`` into ``1.1``, ``01234`` into ``668``, ``NO``
into ``false``). These helpers render again only the keys whose value changed,
appeared or disappeared, and refuse an edit they cannot make that way.

The module sits in ``core`` so that callers outside the MCP layer, such as memory
compaction, can share it without importing ``datacron.mcp``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Final

import yaml

from datacron.core.frontmatter import (
    FRONTMATTER_BOUNDARY_PATTERN,
    FrontmatterError,
    parse,
    parse_preserving_bom_and_body_eols,
    serialize_preserving_bom,
)

__all__ = [
    "FRONTMATTER_EDIT_REFUSED_CODE",
    "FrontmatterEditRefusedError",
    "serialize_preserving_frontmatter",
]

FRONTMATTER_EDIT_REFUSED_CODE: Final[str] = "frontmatter_edit_refused"
_UNLOCATED_FRONTMATTER_MESSAGE: Final[str] = (
    "this note's frontmatter block could not be located exactly, so it cannot be edited "
    "without rewriting it"
)
_UNVERIFIED_FRONTMATTER_MESSAGE: Final[str] = (
    "the edited frontmatter did not read back as requested, so the note was left unchanged"
)
_BOM: Final[str] = "﻿"
_YAML_WHITESPACE: Final[str] = " \t\r\n"
# Plain, single-quoted and double-quoted scalars sit on their key's line and can
# have their value replaced in place; block scalars (| and >) cannot.
_INLINE_SCALAR_STYLES: Final[frozenset[str | None]] = frozenset({None, "", "'", '"'})
_EMPTY_FLOW_MAPPING: Final[str] = "{}"
# Block list items are written two columns deeper than their key, as Obsidian does.
_BLOCK_ITEM_INDENT: Final[int] = 2
# PyYAML folds a long scalar at 80 columns; an edited value must never wrap.
_UNWRAPPED_YAML_WIDTH: Final[int] = sys.maxsize
_UPDATED_KEY: Final[str] = "updated"


class FrontmatterEditRefusedError(ValueError):
    """A frontmatter change that cannot be made without rewriting keys it does not touch.

    Nothing is written when this is raised: the caller keeps the note as it was
    rather than re-dumping the whole block.
    """

    code: Final[str] = FRONTMATTER_EDIT_REFUSED_CODE


@dataclass(frozen=True)
class _FrontmatterEntry:
    """One top-level key of a frontmatter block, located in the block's text."""

    line_start: int
    key_start: int
    key_column: int
    key_text: str
    value: yaml.Node
    end: int
    line_break: str


def serialize_preserving_frontmatter(
    raw: str,
    metadata: dict[str, Any],
    body: str,
    *,
    has_bom: bool,
) -> str:
    """Write ``body`` under the note's own frontmatter text, edited key by key.

    ``raw`` is the note as read; ``metadata`` is its parsed frontmatter after the
    caller's changes. Only the keys whose value changed, appeared or disappeared
    are rendered again; every other byte of the block is kept. The result is read
    back and must yield ``metadata`` exactly, or
    :class:`FrontmatterEditRefusedError` is raised.

    A note without a frontmatter block has nothing to preserve, so one is
    written above its body.
    """
    original, original_body, _had_bom = parse_preserving_bom_and_body_eols(raw)
    if not raw.endswith(original_body):
        raise FrontmatterEditRefusedError(_UNLOCATED_FRONTMATTER_MESSAGE)
    head = raw[: len(raw) - len(original_body)]
    if not head.removeprefix(_BOM):
        if original:
            raise FrontmatterEditRefusedError(_UNLOCATED_FRONTMATTER_MESSAGE)
        return serialize_preserving_bom(metadata, body, has_bom=has_bom)
    changed = [key for key in metadata if key not in original or original[key] != metadata[key]]
    changed.extend(key for key in original if key not in metadata)
    head, eol = _edit_frontmatter_fields(head, metadata, changed)
    if body and not head.endswith(("\n", "\r")):
        # A note that ends on its closing delimiter has no line break after it;
        # the body must not be glued to the delimiter, or the block stops being one.
        head += eol
    result = head + body
    _verify_frontmatter(result, metadata)
    return result


def _edit_frontmatter_fields(
    raw: str, metadata: dict[str, Any], fields: list[str]
) -> tuple[str, str]:
    """Replace, add or remove the selected keys; return the text and its line ending."""
    start, end, eol = _frontmatter_header_span(raw)
    header = raw[start:end]
    header = _without_empty_flow_mapping(header)
    entries, duplicates = _frontmatter_entries(header)
    anchors = _anchor_positions(header)
    touched = dict.fromkeys([*fields, *([_UPDATED_KEY] if _UPDATED_KEY in metadata else [])])
    edits: list[tuple[int, int, str]] = []
    additions = ""
    for field in touched:
        entry = entries.get(field)
        if entry is None:
            if field not in metadata:
                raise FrontmatterEditRefusedError(
                    f"{field!r} is not written as a top-level key of this note's frontmatter "
                    "(it may come from a YAML merge key), so it cannot be removed"
                )
            additions += f"{field}: {_render_flow_value(metadata[field])}{eol}"
            continue
        if field in duplicates:
            raise FrontmatterEditRefusedError(
                f"{field!r} is written more than once in this note's frontmatter"
            )
        if any(entry.line_start <= position < entry.end for position in anchors):
            raise FrontmatterEditRefusedError(
                f"{field!r} carries or uses a YAML anchor or alias in this note's frontmatter; "
                "editing it would change the keys that share its value"
            )
        if field not in metadata:
            edits.append((entry.line_start, entry.end, ""))
        else:
            edits.append(_render_entry_edit(entry, metadata[field], eol))
    for begin, finish, replacement in sorted(edits, reverse=True):
        header = header[:begin] + replacement + header[finish:]
    return raw[:start] + header + additions + raw[end:], eol


def _verify_frontmatter(result: str, metadata: dict[str, Any]) -> None:
    """Refuse a result whose frontmatter does not read back as ``metadata``."""
    try:
        parsed, _ = parse(result)
    except FrontmatterError as exc:
        raise FrontmatterEditRefusedError(_UNVERIFIED_FRONTMATTER_MESSAGE) from exc
    if parsed != metadata:
        raise FrontmatterEditRefusedError(_UNVERIFIED_FRONTMATTER_MESSAGE)


def _frontmatter_header_span(raw: str) -> tuple[int, int, str]:
    """Locate the YAML header between its boundaries; return its span and line ending."""
    offset = 1 if raw.startswith(_BOM) else 0
    lines = raw[offset:].splitlines(keepends=True)
    opening = next((i for i, line in enumerate(lines) if line.strip()), None)
    if opening is None or FRONTMATTER_BOUNDARY_PATTERN.fullmatch(lines[opening]) is None:
        raise FrontmatterEditRefusedError("this edit requires an explicit YAML frontmatter mapping")
    closing = next(
        (
            i
            for i in range(opening + 1, len(lines))
            if FRONTMATTER_BOUNDARY_PATTERN.fullmatch(lines[i]) is not None
        ),
        None,
    )
    if closing is None:
        raise FrontmatterEditRefusedError("this edit requires closed YAML frontmatter")
    start = offset + sum(map(len, lines[: opening + 1]))
    end = offset + sum(map(len, lines[:closing]))
    eol = "\r\n" if lines[opening].endswith("\r\n") else "\n"
    return start, end, eol


def _without_empty_flow_mapping(header: str) -> str:
    """Drop the ``{}`` line that ``serialize`` writes for empty metadata.

    An empty flow mapping holds nothing to preserve, and keys can only be added
    to a block mapping, so its line is removed before the new keys are appended.
    """
    try:
        node = yaml.compose(header, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return header
    if not isinstance(node, yaml.MappingNode) or not node.flow_style or node.value:
        return header
    line_start = header.rfind("\n", 0, node.start_mark.index) + 1
    newline = header.find("\n", node.end_mark.index)
    line_end = len(header) if newline < 0 else newline + 1
    if header[line_start:line_end].strip() != _EMPTY_FLOW_MAPPING:
        return header
    return header[:line_start] + header[line_end:]


def _frontmatter_entries(header: str) -> tuple[dict[Any, _FrontmatterEntry], set[Any]]:
    """Locate each top-level key of ``header``; also return the keys written twice."""
    try:
        node = yaml.compose(header, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise FrontmatterEditRefusedError("this note's frontmatter is not valid YAML") from exc
    if node is None:
        return {}, set()
    if not isinstance(node, yaml.MappingNode) or node.flow_style:
        raise FrontmatterEditRefusedError("this note's frontmatter is not a block YAML mapping")
    entries: dict[Any, _FrontmatterEntry] = {}
    duplicates: set[Any] = set()
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            # A complex key has no name a caller could pass; it is never touched.
            continue
        key_start, key_end = key_node.start_mark.index, key_node.end_mark.index
        try:
            key = yaml.safe_load(header[key_start:key_end])
        except yaml.YAMLError:
            continue
        if key in entries:
            duplicates.add(key)
        content_end = _node_content_end(value_node)
        if content_end is None:
            content_end = key_end
        while content_end > key_end and header[content_end - 1] in _YAML_WHITESPACE:
            content_end -= 1
        newline = header.find("\n", content_end)
        end = len(header) if newline < 0 else newline + 1
        line_break = header[end - 1 : end]
        if header[:end].endswith("\r\n"):
            line_break = "\r\n"
        entries[key] = _FrontmatterEntry(
            line_start=header.rfind("\n", 0, key_start) + 1,
            key_start=key_start,
            key_column=key_node.start_mark.column,
            key_text=header[key_start:key_end],
            value=value_node,
            end=end,
            line_break=line_break if line_break.endswith("\n") else "",
        )
    return entries, duplicates


def _node_content_end(node: yaml.Node) -> int | None:
    """Return where the source text of ``node``'s own content ends, or ``None`` if empty.

    A block collection's end mark lies after its terminating line break and
    after any comment lines that follow it, up to the next key, so the end is
    taken from its last child instead. An empty scalar has no content at all:
    its marks sit wherever the parser stopped.
    """
    if isinstance(node, yaml.ScalarNode):
        return node.end_mark.index if node.end_mark.index > node.start_mark.index else None
    if not isinstance(node, yaml.CollectionNode) or node.flow_style or not node.value:
        return node.end_mark.index
    last = node.value[-1]
    if isinstance(node, yaml.MappingNode):
        last_key, last_value = last
        content_end = _node_content_end(last_value)
        return last_key.end_mark.index if content_end is None else content_end
    content_end = _node_content_end(last)
    return last.start_mark.index if content_end is None else content_end


def _anchor_positions(header: str) -> list[int]:
    """Return where each YAML anchor or alias of ``header`` starts."""
    return [
        token.start_mark.index
        for token in yaml.scan(header, Loader=yaml.SafeLoader)
        if isinstance(token, (yaml.AnchorToken, yaml.AliasToken))
    ]


def _render_entry_edit(entry: _FrontmatterEntry, value: Any, eol: str) -> tuple[int, int, str]:
    """Return the span of ``entry`` to replace, and the text that replaces it.

    A written plain or quoted scalar, or a flow collection, has its value
    replaced in place, which keeps the key and any comment after the value. An
    empty value, a block scalar and a block collection are replaced whole, from
    the key to the end of the value's last line. A block collection keeps its
    block style.
    """
    node = entry.value
    if (
        isinstance(node, yaml.ScalarNode)
        and node.style in _INLINE_SCALAR_STYLES
        and node.end_mark.index > node.start_mark.index
    ) or (isinstance(node, yaml.CollectionNode) and node.flow_style):
        return node.start_mark.index, node.end_mark.index, _render_flow_value(value)
    if isinstance(node, yaml.CollectionNode) and isinstance(value, (list, dict)) and value:
        indent = " " * (entry.key_column + _BLOCK_ITEM_INDENT)
        dumped = yaml.safe_dump(
            value,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=_UNWRAPPED_YAML_WIDTH,
        )
        items = eol.join(indent + line for line in dumped.splitlines())
        return entry.key_start, entry.end, f"{entry.key_text}:{eol}{items}{entry.line_break}"
    rendered = _render_flow_value(value)
    return entry.key_start, entry.end, f"{entry.key_text}: {rendered}{entry.line_break}"


def _render_flow_value(value: Any) -> str:
    """Render ``value`` as YAML on a single line, to follow ``key: `` in place."""
    rendered = yaml.safe_dump(
        value, default_flow_style=True, allow_unicode=True, width=_UNWRAPPED_YAML_WIDTH
    )
    rendered = rendered.removesuffix("...\n").rstrip("\n")
    if "\n" in rendered:
        # A string holding a line break is dumped over several lines; its
        # double-quoted form escapes the break and stays on one.
        rendered = yaml.safe_dump(
            value,
            default_flow_style=True,
            default_style='"',
            allow_unicode=True,
            width=_UNWRAPPED_YAML_WIDTH,
        ).rstrip("\n")
    if "\n" in rendered:
        raise FrontmatterEditRefusedError("the new value cannot be written on a single line")
    return rendered
