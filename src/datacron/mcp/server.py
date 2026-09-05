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
"""MCPServer stdio entry point for Datacron.

Construction is split in two so tests and the CLI can share the wiring:

- :func:`build_app` -- given a :class:`Settings`, a vault root, and the
  Protocol-typed dependencies, returns a :class:`DatacronApp` bundle.
- :func:`create_server` -- wraps the app in a configured :class:`MCPServer`
  instance with tools and resources registered. Adds a lifespan that
  logs startup and shutdown.
- :func:`run_stdio` -- top-level coroutine the CLI awaits. Configures
  logging, builds the app, runs the stdio loop, and ensures clean
  shutdown.

Tool error handling: every tool catches broad exceptions, logs a full
traceback via :func:`datacron.core.logger.get_logger`, and returns a
structured ``{"error": ...}`` payload. :class:`DatacronMCPServer` maps that
payload to an MCP tool result with ``is_error=true`` while keeping the JSON
payload intact in text content.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps
from inspect import isawaitable
from pathlib import Path
from typing import Any, Final, TypeVar, final

from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    CallToolResult,
    Icon,
    InputRequiredResult,
    TextContent,
    ToolAnnotations,
)
from pydantic import AnyUrl

from datacron import __version__
from datacron.core.config import Settings, VaultConfig, get_settings, load_vault_config
from datacron.core.durability import (
    DurabilityStatus,
    WritePolicy,
    probe_directory_durability,
)
from datacron.core.logger import configure_logging, get_logger, shutdown_logging
from datacron.core.memory_protocol import PROTOCOL_BLOCK
from datacron.core.paths import assert_within_read_paths, sidecar_index_db, sidecar_vault_config
from datacron.core.protocols import (
    ASTChunker,
    FTS5Store,
    RipgrepWrapper,
    VaultReader,
    VaultWriter,
)
from datacron.core.scope import (
    ConjunctiveVaultScope,
    NoteAdmissionPolicy,
    ScopedVaultReader,
    ScopedVaultWriter,
    SingleTenantVaultScope,
    VaultScope,
)
from datacron.core.security import SecretRedactor
from datacron.core.vault import SKIPPED_FOLDERS, build_configured_reader
from datacron.core.vault_writer import OperationRecoveryError, VaultLockBusyError
from datacron.mcp.identity import CallerIdentityProvider, StdioCallerIdentityProvider

_ToolCallable = TypeVar("_ToolCallable", bound=Callable[..., Any])

__all__ = [
    "DatacronApp",
    "build_app",
    "create_server",
    "run_stdio",
]

_LOGGER = get_logger(__name__)

SERVER_NAME: Final[str] = "datacron"
SERVER_INSTRUCTIONS: Final[str] = PROTOCOL_BLOCK


@final
@dataclass
class RepairState:
    """Mutable monotonic state for repair-on-read throttling."""

    last_sweep_completed_at: float | None = None


@final
@dataclass(frozen=True)
class DatacronApp:
    """Bundle of resolved dependencies shared by tools and resources.

    Built once at startup and held in the MCPServer lifespan context so each
    tool invocation can read the same VaultReader, chunker, store, ripgrep
    wrapper, and Settings.

    The ``store`` is constructed unopened by :func:`build_app`; the
    lifespan in :func:`create_server` opens it on startup and closes it
    on shutdown.
    """

    settings: Settings
    vault_root: Path
    vault_reader: VaultReader
    chunker: ASTChunker
    store: FTS5Store
    vault_writer: VaultWriter
    ripgrep: RipgrepWrapper
    scope: VaultScope
    identity_provider: CallerIdentityProvider
    secret_redactor: SecretRedactor
    durability_status: DurabilityStatus
    write_policy: WritePolicy
    reconcile_lock: asyncio.Lock
    repair_state: RepairState


@final
class _StructuredToolPayloadError(Exception):
    """Carry a Datacron error payload across MCPServer's public call boundary."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("Datacron structured tool error")
        self.payload = payload


@final
class DatacronMCPServer(MCPServer[DatacronApp]):
    """MCPServer boundary preserving Datacron's public error contracts."""

    def tool(
        self,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> Callable[[_ToolCallable], _ToolCallable]:
        """Register a tool through MCPServer's public decorator with error wrapping."""
        register = super().tool(
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=structured_output,
        )

        def decorator(function: _ToolCallable) -> _ToolCallable:
            @wraps(function)
            async def wrapped(*args: Any, **kwargs: Any) -> Any:
                pending = function(*args, **kwargs)
                result = await pending if isawaitable(pending) else pending
                if _is_structured_tool_error(result):
                    raise _StructuredToolPayloadError(result)
                return result

            register(wrapped)
            return function

        return decorator

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[DatacronApp, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        """Delegate publicly while preserving stable structured tool errors."""
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            cause = exc.__cause__
            if isinstance(cause, _StructuredToolPayloadError):
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps(cause.payload, ensure_ascii=True, sort_keys=True),
                        )
                    ],
                    is_error=True,
                )
            if cause is None:
                raise MCPError(INVALID_PARAMS, f"Unknown tool: {name}") from exc
            raise

    async def read_resource(
        self,
        uri: AnyUrl | str,
        context: Context[DatacronApp, Any] | None = None,
    ) -> Iterable[ReadResourceContents] | InputRequiredResult:
        """Map public MCPServer resource errors to stable JSON-RPC codes."""
        try:
            return await super().read_resource(uri, context)
        except ResourceNotFoundError as exc:
            raise MCPError(INVALID_PARAMS, f"Unknown resource: {uri}") from exc
        except ResourceError as exc:
            raise MCPError(INTERNAL_ERROR, "Resource read failed") from exc
        except Exception as exc:
            raise MCPError(INTERNAL_ERROR, "Resource read failed") from exc


def _is_structured_tool_error(result: object) -> bool:
    """Return whether a tool implementation produced the stable error payload."""
    if not isinstance(result, dict):
        return False
    error = result.get("error")
    return (
        isinstance(error, dict)
        and isinstance(error.get("type"), str)
        and isinstance(error.get("message"), str)
    )


def build_app(
    *,
    settings: Settings | None = None,
    vault_root: Path,
    vault_reader: VaultReader | None = None,
    chunker: ASTChunker | None = None,
    store: FTS5Store | None = None,
    vault_writer: VaultWriter | None = None,
    ripgrep: RipgrepWrapper | None = None,
    scope: VaultScope | None = None,
    identity_provider: CallerIdentityProvider | None = None,
    secret_redactor: SecretRedactor | None = None,
    durability_status: DurabilityStatus | None = None,
) -> DatacronApp:
    """Resolve dependencies into a :class:`DatacronApp` bundle.

    Args:
        settings: Datacron runtime config. Defaults to the cached singleton.
        vault_root: Absolute path to the vault root. Required.
        vault_reader: Optional pre-built :class:`VaultReader`. Defaults to
            the configured filesystem reader bound to ``vault_root``.
        chunker: Optional pre-built :class:`ASTChunker`. Defaults to
            ``MarkdownChunker``.
        store: Optional pre-built :class:`FTS5Store`. Defaults to a fresh
            ``SQLiteFTS5Store()`` (unopened -- the lifespan calls ``open``).
        vault_writer: Optional pre-built :class:`VaultWriter`. Defaults to
            the configured filesystem writer bound to ``vault_root``.
        ripgrep: Optional pre-built :class:`RipgrepWrapper`. Defaults to
            a fresh ``RipgrepWrapper()`` (stateless).

    Concrete indexing classes are imported lazily inside the function so a
    test that supplies its own doubles never triggers the heavyweight
    aiosqlite/mistletoe imports.
    """
    resolved_settings = settings or get_settings()
    resolved_root = vault_root.expanduser().resolve()
    if resolved_settings.read_paths:
        # Empty read_paths keeps vault_root as the implicit boundary; an
        # explicit allowlist must contain the served vault root.
        assert_within_read_paths(resolved_root, resolved_settings)
    vault_config = load_vault_config(sidecar_vault_config(resolved_root)) or VaultConfig()
    admission_policy = NoteAdmissionPolicy(
        excluded_folders=SKIPPED_FOLDERS | frozenset(vault_config.excluded_folders),
        excluded_files=frozenset(vault_config.excluded_files),
    )
    canonical_scope = SingleTenantVaultScope(
        resolved_root,
        resolved_settings,
        admission_policy,
    )
    resolved_scope: VaultScope = (
        canonical_scope if scope is None else ConjunctiveVaultScope(canonical_scope, scope)
    )
    resolved_durability = durability_status or (
        probe_directory_durability(resolved_root)
        if resolved_root.is_dir()
        else DurabilityStatus(backend="unavailable", directory_flush_supported=False)
    )
    write_policy = WritePolicy(resolved_settings, resolved_durability)
    base_reader = vault_reader or build_configured_reader(
        resolved_root,
        read_only=True,
        admission_policy=admission_policy,
    )
    resolved_reader = ScopedVaultReader(
        base_reader,
        resolved_scope,
        admission_policy=admission_policy,
    )
    if chunker is None:
        from datacron.indexing.chunker import MarkdownChunker  # noqa: PLC0415

        chunker = MarkdownChunker(max_tokens=resolved_settings.chunk_max_tokens)
    if store is None:
        from datacron.indexing.fts5_store import SQLiteFTS5Store  # noqa: PLC0415

        store = SQLiteFTS5Store(term_map=vault_config.query_expansion)
    resolved_reader.bind_note_path_lookup(store.get_note_rel_path)
    if vault_writer is None:
        from datacron.core.vault_writer import FilesystemVaultWriter  # noqa: PLC0415

        vault_writer = FilesystemVaultWriter(
            resolved_root,
            resolved_settings,
            vault_config,
            write_policy=write_policy,
        )
    resolved_writer = ScopedVaultWriter(vault_writer, resolved_scope, write_policy)
    if ripgrep is None:
        from datacron.indexing.ripgrep import RipgrepWrapper as _RipgrepWrapper  # noqa: PLC0415

        ripgrep = _RipgrepWrapper()
    return DatacronApp(
        settings=resolved_settings,
        vault_root=resolved_root,
        vault_reader=resolved_reader,
        chunker=chunker,
        store=store,
        vault_writer=resolved_writer,
        ripgrep=ripgrep,
        scope=resolved_scope,
        identity_provider=identity_provider or StdioCallerIdentityProvider(),
        secret_redactor=secret_redactor or SecretRedactor.from_settings(resolved_settings),
        durability_status=resolved_durability,
        write_policy=write_policy,
        reconcile_lock=asyncio.Lock(),
        repair_state=RepairState(),
    )


async def _startup_recover_operations(app: DatacronApp) -> None:
    """Run write-path recovery at startup without blocking tool registration.

    A contended ``oplog`` advisory lock -- another datacron writer still holding
    it -- must not stall the MCP lifespan: if it did, ``initialize`` would never
    be answered and the client would drop the server with zero tools registered.
    On lock contention we log a warning and defer recovery (retried on the next
    write). Residual operation recovery errors are degraded; all other errors abort startup.
    """
    try:
        recovered = await app.vault_writer.recover_operations()
    except VaultLockBusyError as exc:
        _LOGGER.warning(
            "Startup operation-log recovery deferred: %s; tools will register now "
            "and recovery is retried on the next write",
            exc,
        )
        return
    except OperationRecoveryError as exc:
        _LOGGER.error(
            "Startup operation-log recovery blocked by a residual recovery error: %s; "
            "tools will register and reads remain available",
            exc,
        )
        return
    if recovered:
        _LOGGER.warning("Recovered %d committed operation-log entries", recovered)
    blocked = app.vault_writer.recovery_blocked
    if blocked:
        _LOGGER.error(
            "Startup operation-log recovery blocked count=%d first_operation_id=%s; "
            "tools will register and reads remain available",
            len(blocked),
            blocked[0].operation_id,
        )


def create_server(app: DatacronApp) -> MCPServer[DatacronApp]:
    """Return a fully-wired :class:`MCPServer` bound to ``app``."""
    from datacron.mcp.resources import register_resources  # noqa: PLC0415
    from datacron.mcp.tools import register_tools  # noqa: PLC0415

    @asynccontextmanager
    async def _lifespan(server: MCPServer[DatacronApp]) -> AsyncIterator[DatacronApp]:
        _LOGGER.info(
            "datacron-mcp v%s starting (vault_root=%s)",
            __version__,
            app.vault_root,
        )
        if not app.vault_root.is_dir():
            _LOGGER.error("Vault root %s does not exist or is not a directory", app.vault_root)
            raise FileNotFoundError(f"Vault root not found: {app.vault_root}")
        if app.write_policy.writes_allowed:
            await _startup_recover_operations(app)
        else:
            _LOGGER.info("Writable startup recovery skipped by active write policy")
        db_path = sidecar_index_db(app.vault_root)
        index_read_only = not app.write_policy.writes_allowed
        await app.store.open(db_path, read_only=index_read_only)
        _LOGGER.info(
            "FTS5 store opened at %s (read_only=%s)",
            db_path,
            index_read_only,
        )
        try:
            yield app
        finally:
            try:
                await app.store.close()
            finally:
                _LOGGER.info("datacron-mcp v%s shutting down", __version__)

    server: MCPServer[DatacronApp] = DatacronMCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        lifespan=_lifespan,
    )
    register_tools(server, app)
    register_resources(server, app)
    return server


async def run_stdio(
    *,
    settings: Settings | None = None,
    vault_root: Path,
) -> None:
    """Configure logging once at the server boundary and run the stdio loop.

    This is what ``datacron mcp serve`` and the ``datacron-mcp`` script
    entry call. Keeping configuration here instead of :func:`build_app`
    preserves side-effect-free composition in tests and library consumers.
    The coroutine returns when the client disconnects or the runtime is
    interrupted.
    """
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    app = build_app(settings=resolved_settings, vault_root=vault_root)
    server = create_server(app)
    try:
        await server.run_stdio_async()
    finally:
        shutdown_logging()
