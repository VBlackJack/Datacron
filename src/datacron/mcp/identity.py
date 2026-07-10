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
"""Transport-owned caller attribution and the future remote-auth seam."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, final, runtime_checkable

from mcp.server.fastmcp import Context

__all__ = [
    "CallerIdentity",
    "CallerIdentityProvider",
    "StdioCallerIdentityProvider",
]


@dataclass(frozen=True)
class CallerIdentity:
    """Attribution established outside vault-controlled content."""

    actor: str
    transport: str
    assurance: str
    credential_verified: bool


@runtime_checkable
class CallerIdentityProvider(Protocol):
    """Resolve a caller at the single transport authentication point."""

    def identify(self, context: Context[Any, Any, Any]) -> CallerIdentity:
        """Return transport-derived identity and assurance metadata."""
        ...


@final
class StdioCallerIdentityProvider:
    """Attribute the local process connected to the MCP stdio transport.

    MCP client metadata is self-asserted and therefore useful for attribution,
    not cryptographic authentication. The local OS process boundary supplies the
    assurance for the current single-user deployment.
    """

    def identify(self, context: Context[Any, Any, Any]) -> CallerIdentity:
        actor = "mcp-client:unidentified"
        try:
            client_id = context.client_id
            if client_id:
                actor = f"mcp-client:{client_id}"
            else:
                client_params = context.session.client_params
                if client_params is not None:
                    info = client_params.clientInfo
                    actor = f"mcp-client:{info.name}/{info.version}"
        except (AttributeError, ValueError):
            pass
        return CallerIdentity(
            actor=_clean_actor(actor),
            transport="stdio",
            assurance="local-process",
            credential_verified=False,
        )


def _clean_actor(value: str) -> str:
    return " ".join(value.split())[:200] or "mcp-client:unidentified"
