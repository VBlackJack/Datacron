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
"""Tests for versioned Eval v2 baselines and regression gates."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from datacron.core.config import Settings, VaultConfig
from datacron.core.models import (
    EvalPipeline,
    EvalReport,
    EvalSummary,
    EvalTransport,
)
from datacron.eval.baseline import (
    EvalBaseline,
    baseline_path,
    compare_with_baseline,
    eval_config_hash,
    load_baseline,
    save_baseline,
)


def _summary(*, recall_5: float, ndcg: float) -> EvalSummary:
    return EvalSummary(
        pipeline=EvalPipeline.TOOL,
        transport=EvalTransport.IMPL,
        question_count=2,
        note_recall_at_k={5: recall_5, 10: 0.9, 20: 1.0},
        mrr=0.8,
        ndcg_at_10=ndcg,
        citation_precision=0.5,
        forbidden_violation_rate=0.0,
        latency_p50_ms=10.0,
        latency_p95_ms=20.0,
        total_tokens_returned=100,
        avg_tokens_returned=50.0,
    )


def test_save_and_load_baseline_persists_only_aggregate_metrics(tmp_path: Path) -> None:
    report = EvalReport(summary=_summary(recall_5=0.9, ndcg=0.8), results=[])
    settings = Settings()
    vault_config = VaultConfig()

    saved = save_baseline(report, tmp_path, settings, vault_config)
    loaded = load_baseline(tmp_path)
    raw = json.loads(baseline_path(tmp_path).read_text(encoding="utf-8"))

    assert loaded == saved
    assert loaded.summary.note_recall_at_k[5] == 0.9
    assert loaded.config_hash == eval_config_hash(settings, vault_config)
    assert raw["schema_version"] == 1
    assert "results" not in raw


def test_compare_passes_with_small_deltas() -> None:
    baseline = EvalBaseline(
        datacron_version="2026.0716.00",
        created_at=datetime(2026, 7, 16, tzinfo=UTC),
        config_hash="same",
        summary=_summary(recall_5=0.90, ndcg=0.80),
    )
    report = EvalReport(summary=_summary(recall_5=0.89, ndcg=0.79), results=[])

    comparison = compare_with_baseline(
        report,
        baseline,
        tolerance=0.02,
        config_hash="same",
    )

    assert comparison.passed is True
    assert comparison.regressions == []
    assert comparison.config_hash_matches is True


def test_compare_detects_manually_inflated_baseline_regression(tmp_path: Path) -> None:
    settings = Settings()
    vault_config = VaultConfig()
    report = EvalReport(summary=_summary(recall_5=0.90, ndcg=0.86), results=[])
    save_baseline(report, tmp_path, settings, vault_config)
    target = baseline_path(tmp_path)
    raw = json.loads(target.read_text(encoding="utf-8"))
    raw["config_hash"] = "old-config"
    raw["summary"]["note_recall_at_k"]["5"] = 0.95
    raw["summary"]["ndcg_at_10"] = 0.90
    target.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    baseline = load_baseline(tmp_path)

    comparison = compare_with_baseline(
        report,
        baseline,
        tolerance=0.02,
        config_hash="new-config",
    )

    assert comparison.passed is False
    assert comparison.regressions == ["note_recall_at_5", "ndcg_at_10"]
    assert comparison.deltas["note_recall_at_5"] < -0.02
    assert comparison.deltas["ndcg_at_10"] < -0.02
    assert comparison.config_hash_matches is False


def test_eval_config_hash_changes_with_retrieval_config() -> None:
    default_hash = eval_config_hash(Settings(), VaultConfig())
    changed_hash = eval_config_hash(Settings(max_result_tokens=4000), VaultConfig())

    assert default_hash != changed_hash
