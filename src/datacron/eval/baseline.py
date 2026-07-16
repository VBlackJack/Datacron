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
"""Versioned local baselines and regression comparison for Eval v2."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from datacron import __version__
from datacron.core.config import (
    CONFIDENCE_PENALTY,
    SUPERSEDED_DEMOTION_FACTOR,
    Settings,
    VaultConfig,
)
from datacron.core.models import EvalReport, EvalSummary
from datacron.core.paths import sidecar_dir

__all__ = [
    "BaselineComparison",
    "EvalBaseline",
    "baseline_path",
    "compare_with_baseline",
    "eval_config_hash",
    "load_baseline",
    "save_baseline",
]

_BASELINE_SCHEMA_VERSION: Final[int] = 1
_BASELINE_REL_PATH: Final[Path] = Path("eval") / "baseline.json"
_ENCODING_UTF8: Final[str] = "utf-8"


class EvalBaseline(BaseModel):
    """Persisted aggregate metrics plus provenance for one local golden set."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = _BASELINE_SCHEMA_VERSION
    datacron_version: str
    created_at: datetime
    config_hash: str
    summary: EvalSummary


class BaselineComparison(BaseModel):
    """Metric deltas and the two quality-gate regressions."""

    model_config = ConfigDict(frozen=True)

    baseline_version: str
    current_version: str
    config_hash_matches: bool
    mode_matches: bool
    tolerance: float
    deltas: dict[str, float]
    regressions: list[str]
    passed: bool


def baseline_path(vault_root: Path) -> Path:
    """Return the canonical vault-local baseline path."""
    return sidecar_dir(vault_root) / _BASELINE_REL_PATH


def eval_config_hash(settings: Settings, vault_config: VaultConfig) -> str:
    """Hash only configuration that can affect retrieval or serialized results."""
    payload = {
        "settings": {
            "chunk_max_tokens": settings.chunk_max_tokens,
            "max_result_count": settings.max_result_count,
            "max_result_tokens": settings.max_result_tokens,
            "read_only": settings.read_only,
            "redact_secrets": settings.redact_secrets,
            "secret_redaction_patterns": settings.secret_redaction_patterns,
            "write_scope_configured": bool(settings.write_paths),
        },
        "vault": {
            "encoding": vault_config.encoding,
            "excluded_files": vault_config.excluded_files,
            "excluded_folders": vault_config.excluded_folders,
            "query_expansion": vault_config.query_expansion,
        },
        "temporal": {
            "confidence_penalty": CONFIDENCE_PENALTY,
            "superseded_demotion_factor": SUPERSEDED_DEMOTION_FACTOR,
        },
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode(_ENCODING_UTF8)
    return hashlib.sha256(canonical).hexdigest()


def save_baseline(
    report: EvalReport,
    vault_root: Path,
    settings: Settings,
    vault_config: VaultConfig,
) -> EvalBaseline:
    """Atomically persist aggregate metrics in the vault sidecar."""
    baseline = EvalBaseline(
        datacron_version=__version__,
        created_at=datetime.now(UTC),
        config_hash=eval_config_hash(settings, vault_config),
        summary=report.summary,
    )
    target = baseline_path(vault_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    serialized = json.dumps(
        baseline.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    temporary.write_text(f"{serialized}\n", encoding=_ENCODING_UTF8)
    temporary.replace(target)
    return baseline


def load_baseline(vault_root: Path) -> EvalBaseline:
    """Load and validate the vault-local baseline."""
    target = baseline_path(vault_root)
    return EvalBaseline.model_validate_json(target.read_text(encoding=_ENCODING_UTF8))


def compare_with_baseline(
    report: EvalReport,
    baseline: EvalBaseline,
    *,
    tolerance: float,
    config_hash: str,
) -> BaselineComparison:
    """Compare aggregate metrics and flag recall@5 or nDCG@10 regressions."""
    current = report.summary
    previous = baseline.summary
    deltas = _metric_deltas(current, previous)
    regressions: list[str] = []
    recall_delta = deltas.get("note_recall_at_5", 0.0)
    if recall_delta < -tolerance:
        regressions.append("note_recall_at_5")
    ndcg_delta = deltas["ndcg_at_10"]
    if ndcg_delta < -tolerance:
        regressions.append("ndcg_at_10")
    return BaselineComparison(
        baseline_version=baseline.datacron_version,
        current_version=__version__,
        config_hash_matches=baseline.config_hash == config_hash,
        mode_matches=(
            previous.pipeline is current.pipeline and previous.transport is current.transport
        ),
        tolerance=tolerance,
        deltas=deltas,
        regressions=regressions,
        passed=not regressions,
    )


def _metric_deltas(current: EvalSummary, previous: EvalSummary) -> dict[str, float]:
    """Return current-minus-baseline deltas for every comparable aggregate."""
    deltas = {
        f"note_recall_at_{k}": current.note_recall_at_k.get(k, 0.0)
        - previous.note_recall_at_k.get(k, 0.0)
        for k in sorted(set(current.note_recall_at_k) | set(previous.note_recall_at_k))
    }
    deltas.update(
        {
            "mrr": current.mrr - previous.mrr,
            "ndcg_at_10": current.ndcg_at_10 - previous.ndcg_at_10,
            "citation_precision": current.citation_precision - previous.citation_precision,
            "latency_p50_ms": current.latency_p50_ms - previous.latency_p50_ms,
            "latency_p95_ms": current.latency_p95_ms - previous.latency_p95_ms,
            "total_tokens_returned": float(
                current.total_tokens_returned - previous.total_tokens_returned
            ),
            "avg_tokens_returned": current.avg_tokens_returned - previous.avg_tokens_returned,
        }
    )
    if (
        current.forbidden_violation_rate is not None
        and previous.forbidden_violation_rate is not None
    ):
        deltas["forbidden_violation_rate"] = (
            current.forbidden_violation_rate - previous.forbidden_violation_rate
        )
    _add_chunk_recall_deltas(deltas, current, previous)
    return deltas


def _add_chunk_recall_deltas(
    deltas: dict[str, float],
    current: EvalSummary,
    previous: EvalSummary,
) -> None:
    """Add chunk recall only when both runs contain chunk ground truth."""
    if current.chunk_recall_at_k is None or previous.chunk_recall_at_k is None:
        return
    for k in sorted(set(current.chunk_recall_at_k) | set(previous.chunk_recall_at_k)):
        deltas[f"chunk_recall_at_{k}"] = current.chunk_recall_at_k.get(
            k, 0.0
        ) - previous.chunk_recall_at_k.get(k, 0.0)
