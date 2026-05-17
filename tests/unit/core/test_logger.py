# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.core.logger`."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.core.logger import configure_logging, get_logger, shutdown_logging


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
