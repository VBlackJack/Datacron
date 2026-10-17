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
"""Thread-safe daily file logger for the Datacron runtime.

Format: ``[YYYY-MM-DD HH:MM:SS] [LEVEL] message``. One file per day in
``$DATACRON_LOG_DIR/datacron_YYYYMMDD.log`` (default
``~/.datacron/logs/``). A background :class:`QueueListener` drains records to
the file and to stderr (WARNING and above). All loggers obtained via
:func:`get_logger` route through the same queue.

Importing this module or calling :func:`get_logger` never configures logging.
Until an entrypoint calls :func:`configure_logging`, ``datacron.*`` loggers have
no handlers of their own and propagate to Python's standard root logger.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import queue
import sys
import threading
from datetime import UTC, date, datetime, timedelta
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from typing import Any, Final

from datacron.core.config import (
    DEFAULT_LOG_RETENTION_DAYS,
    LOG_DATE_FORMAT,
    LOG_FILENAME_PATTERN,
    LOG_FORMAT,
    Settings,
    get_settings,
)
from datacron.core.security import SecretRedactor

__all__ = ["configure_logging", "get_logger", "shutdown_logging"]

_ROOT_LOGGER_NAME: Final[str] = "datacron"
_setup_lock = threading.Lock()
_listener: QueueListener | None = None
_log_queue: queue.Queue[logging.LogRecord] | None = None
_configured: bool = False


class _RedactingFormatter(logging.Formatter):
    """Apply secret redaction to the fully rendered record, including exceptions."""

    def __init__(self, redactor: SecretRedactor | None) -> None:
        super().__init__(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        if self._redactor is None:
            return rendered
        return self._redactor.redact_text(rendered)


def _log_date() -> date:
    return datetime.now(tz=UTC).astimezone().date()


class _DailyFileHandler(logging.FileHandler):
    """Append to the current local day's file and prune dated logs across restarts."""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._day = _log_date()
        super().__init__(self._path(self._day), encoding="utf-8", delay=True)
        self._prune()

    def _path(self, day: date) -> Path:
        return self._log_dir / LOG_FILENAME_PATTERN.format(date=day.strftime("%Y%m%d"))

    def _prune(self) -> None:
        cutoff = self._day - timedelta(days=DEFAULT_LOG_RETENTION_DAYS)
        for path in self._log_dir.glob(LOG_FILENAME_PATTERN.format(date="*")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                prefix, _, suffix = LOG_FILENAME_PATTERN.partition("{date}")
                day = datetime.strptime(
                    path.name.removeprefix(prefix).removesuffix(suffix), "%Y%m%d"
                ).date()
            except ValueError:
                continue
            # Exact round-trip prevents permissive date parsing from deleting other names.
            if path.name == self._path(day).name and day < cutoff:
                path.unlink()

    def emit(self, record: logging.LogRecord) -> None:
        day = _log_date()
        if day != self._day:
            try:
                self.close()
                self._day = day
                self.baseFilename = str(self._path(day).resolve())
                self._prune()
            except OSError:
                self.handleError(record)
        super().emit(record)


def _build_file_handler(
    log_dir: Path,
    redactor: SecretRedactor | None,
) -> logging.FileHandler:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = _DailyFileHandler(log_dir)
    handler.setFormatter(_RedactingFormatter(redactor))
    handler.setLevel(logging.DEBUG)
    return handler


def _build_stderr_handler(redactor: SecretRedactor | None) -> logging.StreamHandler[Any]:
    handler: logging.StreamHandler[Any] = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_RedactingFormatter(redactor))
    handler.setLevel(logging.WARNING)
    return handler


def configure_logging(settings: Settings | None = None) -> None:
    """Initialize the queue-based logging pipeline.

    Idempotent: a second call is a no-op until :func:`shutdown_logging` runs.
    """
    global _listener, _log_queue, _configured  # noqa: PLW0603

    with _setup_lock:
        if _configured:
            return
        resolved = settings or get_settings()
        level = getattr(logging, resolved.log_level, logging.INFO)
        redactor = (
            SecretRedactor.from_settings(resolved) if SecretRedactor.log_enabled(resolved) else None
        )

        log_queue: queue.Queue[logging.LogRecord] = queue.Queue(-1)
        file_handler = _build_file_handler(resolved.log_dir, redactor)
        stderr_handler = _build_stderr_handler(redactor)

        listener = QueueListener(
            log_queue,
            file_handler,
            stderr_handler,
            respect_handler_level=True,
        )
        listener.start()

        root = logging.getLogger(_ROOT_LOGGER_NAME)
        root.handlers.clear()
        root.addHandler(QueueHandler(log_queue))
        root.setLevel(level)
        root.propagate = False

        _log_queue = log_queue
        _listener = listener
        _configured = True
        atexit.register(shutdown_logging)


def shutdown_logging() -> None:
    """Flush and stop the background listener. Safe to call multiple times."""
    global _listener, _log_queue, _configured  # noqa: PLW0603

    with _setup_lock:
        listener = _listener
        _listener = None
        _log_queue = None
        _configured = False

        root = logging.getLogger(_ROOT_LOGGER_NAME)
        for handler in list(root.handlers):
            with contextlib.suppress(Exception):
                handler.close()
        root.handlers.clear()
        # Restore the documented unconfigured state so callers such as pytest's
        # root capture handler can observe subsequent Datacron records.
        root.propagate = True

        if listener is not None:
            listener.stop()
            for handler in listener.handlers:
                try:
                    try:
                        handler.flush()
                    except ValueError:
                        # CliRunner and other embedders may close their captured stderr
                        # before Datacron tears down the StreamHandler that references it.
                        stream = getattr(handler, "stream", None)
                        if stream is None or not getattr(stream, "closed", False):
                            raise
                finally:
                    try:
                        handler.close()
                    except ValueError:
                        stream = getattr(handler, "stream", None)
                        if stream is None or not getattr(stream, "closed", False):
                            raise


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``datacron`` namespace.

    ``name`` is typically ``__name__``; it is normalized so that any caller
    inside the ``datacron`` package shares explicitly configured handlers
    without duplicating output. This function never parses settings, creates
    files, or starts the queue listener.
    """
    if name.startswith(f"{_ROOT_LOGGER_NAME}."):
        logger_name = name
    elif name == _ROOT_LOGGER_NAME:
        logger_name = _ROOT_LOGGER_NAME
    else:
        logger_name = f"{_ROOT_LOGGER_NAME}.{name}"
    return logging.getLogger(logger_name)
