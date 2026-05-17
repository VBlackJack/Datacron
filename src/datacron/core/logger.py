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
"""

from __future__ import annotations

import atexit
import logging
import queue
import sys
import threading
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Final

from datacron.core.config import (
    LOG_DATE_FORMAT,
    LOG_FILENAME_PATTERN,
    LOG_FORMAT,
    Settings,
    get_settings,
)

__all__ = ["configure_logging", "get_logger", "shutdown_logging"]

_ROOT_LOGGER_NAME: Final[str] = "datacron"
_setup_lock = threading.Lock()
_listener: QueueListener | None = None
_log_queue: queue.Queue[logging.LogRecord] | None = None
_configured: bool = False


def _build_file_handler(log_dir: Path) -> TimedRotatingFileHandler:
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(tz=UTC).astimezone().strftime("%Y%m%d")
    log_path = log_dir / LOG_FILENAME_PATTERN.format(date=today)
    handler = TimedRotatingFileHandler(
        filename=log_path,
        when="midnight",
        backupCount=14,
        encoding="utf-8",
        delay=True,
        utc=False,
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    handler.setLevel(logging.DEBUG)
    return handler


def _build_stderr_handler() -> logging.StreamHandler[Any]:
    handler: logging.StreamHandler[Any] = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
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

        log_queue: queue.Queue[logging.LogRecord] = queue.Queue(-1)
        file_handler = _build_file_handler(resolved.log_dir)
        stderr_handler = _build_stderr_handler()

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
            try:
                handler.close()
            except Exception:  # noqa: BLE001 — never fail teardown
                pass
        root.handlers.clear()

        if listener is not None:
            listener.stop()
            for handler in listener.handlers:
                try:
                    handler.flush()
                finally:
                    handler.close()


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``datacron`` namespace.

    ``name`` is typically ``__name__``; it is normalized so that any caller
    inside the ``datacron`` package shares the configured handlers without
    duplicating output.
    """
    if not _configured:
        configure_logging()

    if name.startswith(f"{_ROOT_LOGGER_NAME}."):
        logger_name = name
    elif name == _ROOT_LOGGER_NAME:
        logger_name = _ROOT_LOGGER_NAME
    else:
        logger_name = f"{_ROOT_LOGGER_NAME}.{name}"
    return logging.getLogger(logger_name)
