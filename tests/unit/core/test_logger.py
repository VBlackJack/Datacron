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
"""Tests for :mod:`datacron.core.logger`."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import pytest

from datacron.core import logger as logger_module
from datacron.core.config import Settings
from datacron.core.logger import configure_logging, get_logger, shutdown_logging


def test_daily_handler_switches_at_midnight_without_renaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = date(2026, 10, 8)
    monkeypatch.setattr(logger_module, "_log_date", lambda: day, raising=False)
    handler = logger_module._build_file_handler(tmp_path, None)
    record = logging.LogRecord("datacron.test", logging.INFO, __file__, 1, "before", (), None)
    try:
        handler.handle(record)
        day += timedelta(days=1)
        record.msg = "after"
        handler.handle(record)
        handler.flush()
    finally:
        handler.close()
    assert "before" in (tmp_path / "datacron_20261008.log").read_text()
    assert "after" not in (tmp_path / "datacron_20261008.log").read_text()
    assert "after" in (tmp_path / "datacron_20261009.log").read_text()


def test_daily_retention_applies_across_restarts_and_preserves_unrelated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = date(2026, 10, 8)
    monkeypatch.setattr(logger_module, "_log_date", lambda: today, raising=False)
    for age in range(21):
        day = today - timedelta(days=age)
        (tmp_path / f"datacron_{day:%Y%m%d}.log").write_text(f"day {age}\n")
    unrelated = tmp_path / "datacron_notes.log"
    unrelated.write_text("keep")
    invalid = tmp_path / "datacron_20261399.log"
    invalid.write_text("keep")
    handler = logger_module._build_file_handler(tmp_path, None)
    handler.close()
    daily = sorted(p.name for p in tmp_path.glob("datacron_2026*.log") if p != invalid)
    assert daily == [
        f"datacron_{today - timedelta(days=age):%Y%m%d}.log" for age in reversed(range(15))
    ]
    assert unrelated.read_text() == invalid.read_text() == "keep"


class TestLogger:
    def test_get_logger_namespace(self) -> None:
        logger = get_logger("foo.bar")
        assert logger.name == "datacron.foo.bar"

    def test_get_logger_root(self) -> None:
        logger = get_logger("datacron")
        assert logger.name == "datacron"

    def test_logging_writes_to_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DATACRON_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("DATACRON_LOG_LEVEL", "DEBUG")
        shutdown_logging()
        settings = Settings()
        configure_logging(settings)

        logger = get_logger("test.module")
        logger.info("hello-file-logger")

        shutdown_logging()
        log_dir = tmp_path / "logs"
        log_files = list(log_dir.glob("datacron_*.log"))
        assert log_files, "Expected at least one log file"
        contents = log_files[0].read_text(encoding="utf-8")
        assert "hello-file-logger" in contents
        assert "[INFO]" in contents

    def test_configure_is_idempotent(self) -> None:
        configure_logging()
        configure_logging()
        root = logging.getLogger("datacron")
        assert len(root.handlers) == 1

    def test_shutdown_restores_root_propagation(self) -> None:
        shutdown_logging()
        configure_logging()
        root = logging.getLogger("datacron")
        assert root.propagate is False

        shutdown_logging()

        assert root.handlers == []
        assert root.propagate is True
