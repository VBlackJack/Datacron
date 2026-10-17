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
"""Localhost-bound HTTP server and desktop app-window launcher for Datacron Companion."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import shutil
import subprocess
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlparse

from datacron.companion.api import (
    get_companion_history,
    get_companion_state,
    list_companion_notes,
    run_companion_compaction,
    run_companion_graph_health,
    run_companion_mcp_setup,
    run_companion_migration,
    run_companion_search,
    run_companion_sync,
    save_companion_topology,
    save_vault_config,
)
from datacron.companion.ui import render_companion_html
from datacron.config_contract import ConfigCasConflictError, ConfigValidationError
from datacron.core.logger import get_logger
from datacron.migration import MigrationError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping
    from pathlib import Path

    PostHandler = Callable[..., Coroutine[Any, Any, dict[str, Any]]]

__all__ = [
    "DEFAULT_COMPANION_HOST",
    "MAX_REQUEST_BODY_BYTES",
    "TOKEN_HEADER_NAME",
    "CompanionHTTPServer",
    "create_companion_server",
    "launch_companion_window",
]

DEFAULT_COMPANION_HOST: Final[str] = "127.0.0.1"
TOKEN_HEADER_NAME: Final[str] = "X-Datacron-Token"  # noqa: S105
MAX_REQUEST_BODY_BYTES: Final[int] = 262_144
_SESSION_TOKEN_BYTES: Final[int] = 24
_HTML_CSP: Final[str] = (
    "default-src 'self'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)
_LOGGER = get_logger(__name__)
_APP_BROWSER_CANDIDATES: Final[tuple[str, ...]] = (
    "msedge",
    "chrome",
    "google-chrome",
    "chromium",
)

_POST_ROUTES: Final[dict[str, PostHandler]] = {
    "/api/topology": save_companion_topology,
    "/api/vault-config": save_vault_config,
    "/api/sync": run_companion_sync,
    "/api/migrate": run_companion_migration,
    "/api/notes": list_companion_notes,
    "/api/search": run_companion_search,
    "/api/graph-health": run_companion_graph_health,
    "/api/compact-memory": run_companion_compaction,
    "/api/mcp-setup": run_companion_mcp_setup,
    "/api/history": get_companion_history,
}


class CompanionHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server holding Companion session state."""

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        session_token: str,
        profile_path: Path | None = None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.session_token = session_token
        self.profile_path = profile_path


class _CompanionRequestHandler(BaseHTTPRequestHandler):
    """Strict localhost request handler with CSRF token verification."""

    def log_message(self, format: str, *args: object) -> None:
        return

    @property
    def _companion_server(self) -> CompanionHTTPServer:
        if not isinstance(self.server, CompanionHTTPServer):
            raise TypeError("Expected CompanionHTTPServer instance")
        return self.server

    def _check_host(self) -> bool:
        host_header = self.headers.get("Host", "").strip().lower()
        if not host_header:
            return False
        port = self._companion_server.server_address[1]
        allowed = {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            "127.0.0.1",
            "localhost",
        }
        return host_header in allowed

    def _check_origin(self) -> bool:
        origin = self.headers.get("Origin", "").strip().lower().rstrip("/")
        if not origin:
            return True
        port = self._companion_server.server_address[1]
        allowed = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
        return origin in allowed

    def _check_token(self) -> bool:
        supplied = self.headers.get(TOKEN_HEADER_NAME, "")
        expected = self._companion_server.session_token
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def _send_json(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html_text: str) -> None:
        raw = html_text.encode("utf-8")
        self.send_response(int(HTTPStatus.OK))
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", _HTML_CSP)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, Any]:
        length_str = self.headers.get("Content-Length", "0")
        length = int(length_str) if length_str.isdigit() else 0
        if length <= 0:
            return {}
        if length > MAX_REQUEST_BODY_BYTES:
            raise ValueError("Request payload exceeds maximum allowed size")
        raw = self.rfile.read(length)
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("JSON request body must be an object")
        return decoded

    def do_GET(self) -> None:
        if not self._check_host():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid Host header"})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = render_companion_html(self._companion_server.session_token)
            self._send_html(html)
            return
        if parsed.path == "/api/state":
            if not self._check_token():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid session token"})
                return
            try:
                state = asyncio.run(get_companion_state(self._companion_server.profile_path))
                self._send_json(HTTPStatus.OK, state)
            except Exception:
                _LOGGER.exception("Companion internal error on %s", parsed.path)
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "Internal server error"},
                )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        if not self._check_host() or not self._check_origin():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid Host or Origin header"})
            return
        parsed = urlparse(self.path)
        if not self._check_token():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid session token"})
            return
        handler = _POST_ROUTES.get(parsed.path)
        if handler is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            body = self._read_json_body()
            result = asyncio.run(handler(body, profile_path=self._companion_server.profile_path))
            self._send_json(HTTPStatus.OK, result)
        except ConfigCasConflictError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except (ConfigValidationError, MigrationError, ValueError, FileNotFoundError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            _LOGGER.exception("Companion internal error on %s", parsed.path)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Internal server error"},
            )


def create_companion_server(
    *,
    host: str = DEFAULT_COMPANION_HOST,
    port: int = 0,
    profile_path: Path | None = None,
    session_token: str | None = None,
) -> tuple[CompanionHTTPServer, str, str]:
    """Create a bound Companion HTTP server and return `(server, url, token)`."""
    if host != DEFAULT_COMPANION_HOST:
        raise ValueError("Datacron Companion must bind exclusively to 127.0.0.1")
    token = session_token or secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
    server = CompanionHTTPServer(
        (host, port),
        _CompanionRequestHandler,
        session_token=token,
        profile_path=profile_path,
    )
    bound_port = server.server_address[1]
    url = f"http://{host}:{bound_port}/"
    return server, url, token


def launch_companion_window(url: str) -> bool:
    """Open the Companion URL in a chromeless desktop app window or default browser."""
    for candidate in _APP_BROWSER_CANDIDATES:
        binary = shutil.which(candidate)
        if binary is not None:
            try:
                subprocess.Popen(  # noqa: S603
                    [binary, f"--app={url}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except OSError:
                continue
    return webbrowser.open(url)
