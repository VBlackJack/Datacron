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
"""Tests for transport-owned MCP caller attribution."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from mcp.server.fastmcp import Context

from datacron.mcp.identity import StdioCallerIdentityProvider


def test_stdio_identity_uses_transport_context_only() -> None:
    context = cast("Context[Any, Any, Any]", SimpleNamespace(client_id="client-42"))
    identity = StdioCallerIdentityProvider().identify(context)
    assert identity.actor == "mcp-client:client-42"
    assert identity.transport == "stdio"
    assert identity.assurance == "local-process"
    assert identity.credential_verified is False


def test_stdio_identity_has_explicit_unidentified_fallback() -> None:
    context = cast("Context[Any, Any, Any]", SimpleNamespace())
    identity = StdioCallerIdentityProvider().identify(context)
    assert identity.actor == "mcp-client:unidentified"
