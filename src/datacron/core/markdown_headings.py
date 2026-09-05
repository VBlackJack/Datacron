# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Shared AST heading identities and physical spans for reading and editing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mistletoe import block_token

_SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")


@dataclass(frozen=True)
class MarkdownHeading:
    """Zero-based heading span, with an exclusive end including the underline."""

    start: int
    end: int
    level: int
    text: str


def token_text(token: Any) -> str:
    """Return the same inline text identity for maps, chunks, and write selectors."""
    children = getattr(token, "children", None) or []
    if children:
        return "".join(token_text(child) for child in children)
    content = getattr(token, "content", None)
    return content if isinstance(content, str) else ""


def markdown_headings(lines: list[str]) -> list[MarkdownHeading]:
    """Select top-level document headings, excluding code, quotes, and list contents."""
    document = block_token.Document(
        [line.replace("\r\n", "\n").replace("\r", "\n") for line in lines]
    )
    result = []
    for token in document.children or []:
        if not isinstance(token, block_token.Heading | block_token.SetextHeading):
            continue
        start = int(getattr(token, "line_number", 1)) - 1
        end = start + 1
        if isinstance(token, block_token.SetextHeading):
            end = next(
                i + 1
                for i in range(start + 1, len(lines))
                if _SETEXT.fullmatch(lines[i].rstrip("\r\n"))
            )
        result.append(MarkdownHeading(start, end, int(token.level), token_text(token).strip()))
    return result


def heading_before(lines: list[str], content_start: int) -> MarkdownHeading:
    """Resolve a previously selected content boundary back to its heading."""
    return next(item for item in markdown_headings(lines) if item.end == content_start)
