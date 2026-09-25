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
"""Describe parser errors by position and rule, never by the offending content.

pydantic's ``str(ValidationError)`` carries each rejected ``input_value`` and
PyYAML's ``str(MarkedYAMLError)`` carries a snippet of the offending line. Both
reached tool results and the audit log verbatim, and the manifest path may name
any JSON file outside the vault: pointing it at a credentials file returned the
secret inside the "extra inputs are not permitted" message. These helpers keep
what a caller needs to fix the document, the location and the rule, and drop
the value.
"""

from __future__ import annotations

import yaml
from pydantic import ValidationError

__all__ = ["describe_config_error", "describe_validation_error", "describe_yaml_error"]


def describe_config_error(exc: ValueError | yaml.YAMLError) -> str:
    """Describe a configuration load failure with the matching value-free helper.

    Args:
        exc: A YAML parse error, a pydantic validation error, or a plain
            ``ValueError`` raised by a loader about the document's shape.

    Returns:
        A description that names the location and the rule, never the value.
    """
    if isinstance(exc, ValidationError):
        return describe_validation_error(exc)
    if isinstance(exc, yaml.YAMLError):
        return describe_yaml_error(exc)
    return str(exc)


def describe_validation_error(exc: ValidationError) -> str:
    """Return one ``location: message`` line per error, without input values.

    Args:
        exc: The pydantic validation error to describe.

    Returns:
        The error count and title, then each error's dotted location and message.
    """
    details = exc.errors(include_url=False, include_input=False, include_context=False)
    lines = [f"{len(details)} validation error(s) for {exc.title}"]
    for detail in details:
        location = ".".join(str(part) for part in detail["loc"]) or "<root>"
        lines.append(f"{location}: {detail['msg']}")
    return "\n".join(lines)


def describe_yaml_error(exc: yaml.YAMLError) -> str:
    """Return the YAML problem and its position, without the source snippet.

    Args:
        exc: The PyYAML error to describe.

    Returns:
        The context and problem with one-based line and column, or the plain
        message for an error raised without a position.
    """
    if not isinstance(exc, yaml.MarkedYAMLError):
        return str(exc)
    parts: list[str] = []
    for message, mark in ((exc.context, exc.context_mark), (exc.problem, exc.problem_mark)):
        if message is None:
            continue
        if mark is None:
            parts.append(message)
        else:
            parts.append(f"{message} (line {mark.line + 1}, column {mark.column + 1})")
    return "; ".join(parts) or type(exc).__name__
