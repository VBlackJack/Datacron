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
"""Markdown AST chunking for Datacron notes.

Implementation outline while waiting for ``datacron.core.models`` to land:

1. Parse ``Note.content`` only. Frontmatter belongs to ``Note.frontmatter`` and is not
   re-chunked here.
2. Use mistletoe, not markdown-it-py, for the Markdown AST. It is already declared by
   the project and exposes a structured block-token tree where fenced code blocks,
   tables, lists, and quotes can be classified before rendering.
3. Walk block tokens in document order and maintain a heading stack for h1-h6.
4. Emit a heading chunk when a heading token is encountered, then update the active
   header trail for following content.
5. Emit code fences, GFM tables, lists, and blockquotes as atomic chunks.
6. Emit narrative chunks for paragraph-like content. Notes without headings split on
   paragraph units; empty notes produce one empty narrative chunk.
7. Build chunk identifiers with a slugged header path, but keep ``Chunk.header_path``
   human-readable per the frozen contract. This intentionally follows
   ``01-contracts.md`` over the conflicting wording in ``03-brief-codex.md``.
8. Track ordinals per ``(note_id, header_path, chunk_type)`` and format the ordinal
   segment as four digits in ``chunk_id``.
9. Compute content hash from LF-normalized chunk content, approximate tokens with the
   contract heuristic, and attach raw wikilink targets with the lightweight chunker
   regex. Full wikilink parsing remains in ``indexing.wikilinks``.
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALPHANUMERIC_PATTERN = re.compile(r"[^a-z0-9]+")
_REPEATED_DASH_PATTERN = re.compile(r"-+")

__all__: list[str] = []


def _slug_header_path(headings: list[str]) -> str:
    """Return the slugged heading path used inside deterministic chunk IDs."""
    return "/".join(_slug_heading(heading) for heading in headings)


def _slug_heading(heading: str) -> str:
    normalized = unicodedata.normalize("NFKD", heading)
    ascii_heading = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_heading.lower()
    replaced = _NON_ALPHANUMERIC_PATTERN.sub("-", lowered)
    collapsed = _REPEATED_DASH_PATTERN.sub("-", replaced)
    return collapsed.strip("-")
