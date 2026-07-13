# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Julien Bombled
"""Measure a cached LLM judge over the frozen LOT 9.5 ranking."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any, cast

from run_measure import digest_candidates, load_detector, pair_key

LOGGER = logging.getLogger("datacron.contradiction.llm_judge_measure")


def load_json(path: Path) -> Any:
    """Load one UTF-8 JSON document."""
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a fixed artifact."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_text(text: str) -> str:
    """Normalize prose for exact few-shot pair comparisons."""
    return re.sub(r"\s+", " ", text.casefold()).strip()


def judgment_by_pair(cache: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Index cached judgments by unordered note pair."""
    return {
        pair_key(*map(str, entry["pair"])): entry
        for entry in cast("dict[str, dict[str, Any]]", cache["entries"]).values()
    }


def anti_leak_findings(
    provider: Any,
    config: dict[str, Any],
    corpus: list[dict[str, Any]],
    ranking: list[dict[str, Any]],
    labels: list[dict[str, Any]],
) -> dict[str, Any]:
    """Scan implementation literals and few-shot pairs against evaluation identities."""
    pool_paths = {str(path) for item in ranking for path in (str(item["a"]), str(item["b"]))}
    labeled_paths = {
        str(path) for item in labels for path in (str(item["note_a"]), str(item["note_b"]))
    }
    forbidden_paths = pool_paths | labeled_paths
    forbidden_ids = {str(item["id"]) for item in corpus if str(item["path"]) in forbidden_paths}
    literals = sorted(forbidden_paths | forbidden_ids, key=lambda value: (-len(value), value))
    literal_findings: list[dict[str, str]] = []
    scan_paths = [Path(str(path)) for path in cast("list[str]", config["anti_leak_scan_paths"])]
    for path in scan_paths:
        text = path.read_text(encoding="utf-8")
        for literal in literals:
            if literal in text:
                literal_findings.append({"path": path.as_posix(), "literal": literal})

    prompt_text = Path(str(config["prompt_path"])).read_text(encoding="utf-8")
    few_shots = cast("list[dict[str, Any]]", provider.extract_few_shots(prompt_text))
    note_text_by_path = {
        str(item["path"]): normalized_text(f"{item.get('title', '')}\n{item.get('body', '')}")
        for item in corpus
    }
    evaluation_pairs = {
        tuple(
            sorted(
                (
                    note_text_by_path[str(item["a"])],
                    note_text_by_path[str(item["b"])],
                )
            )
        )
        for item in ranking
    }
    evaluation_pairs.update(
        tuple(
            sorted(
                (
                    note_text_by_path[str(item["note_a"])],
                    note_text_by_path[str(item["note_b"])],
                )
            )
        )
        for item in labels
    )
    few_shot_findings = []
    for index, item in enumerate(few_shots, 1):
        key = tuple(
            sorted(
                (
                    normalized_text(str(item["note_a"])),
                    normalized_text(str(item["note_b"])),
                )
            )
        )
        if key in evaluation_pairs:
            few_shot_findings.append({"few_shot": index, "reason": "exact_pair_match"})
    return {
        "scan_paths": [path.as_posix() for path in scan_paths],
        "pool_path_literals_checked": len(pool_paths),
        "labeled_path_literals_checked": len(labeled_paths),
        "note_id_literals_checked": len(forbidden_ids),
        "few_shots_checked": len(few_shots),
        "literal_findings": literal_findings,
        "few_shot_findings": few_shot_findings,
        "pass": not literal_findings and not few_shot_findings,
    }


def coverage_record(
    label: dict[str, Any],
    entry: dict[str, Any],
    retained: bool,
    stratum: str | None,
) -> dict[str, Any]:
    """Describe one adjudicated pair and its cached judge decision."""
    judgment = cast("dict[str, Any]", entry["judgment"])
    return {
        "note_a": str(label["note_a"]),
        "note_b": str(label["note_b"]),
        "label": int(label["label"]),
        "label_source": str(label["source"]),
        "stratum": stratum,
        "retained": retained,
        "judgment": judgment,
        "cache_key": str(entry["cache_key"]),
        "frozen_rank": int(entry["frozen_rank"]),
    }


def render_report(evidence: dict[str, Any]) -> str:
    """Render the French measurement-first LOT 10 report."""
    metrics = evidence["adjudicated_metrics"]
    backlog = evidence["unjudged_backlog"]
    gates = evidence["gates"]
    status = "MESURE VALIDE" if gates["measurement_valid"] else "STOP"
    model_versions = ", ".join(f"`{item}`" for item in evidence["model"]["observed_versions"])
    recall_status = "PASS" if gates["recall_preservation"]["pass"] else "FAIL"
    lines = [
        "# Mesure du LLM-juge sur ranking figé",
        "",
        "Date : 2026-07-13",
        f"Statut : {status} — aucune décision de production",
        "",
        "## Verdict",
        "",
        (
            f"Sur les {metrics['adjudicated_pairs']} paires adjugées, le juge conserve "
            f"{metrics['true_positives']} vraies contradictions et "
            f"{metrics['false_positives']} négatives connues. Sa précision est "
            f"{metrics['precision']:.3f}, contre {metrics['keep_all_precision']:.3f} pour "
            "le baseline keep-all."
        ),
        (
            f"Rappel-juge : {metrics['true_positives']}/{metrics['positive_total']} = "
            f"{metrics['judge_recall']:.3f}. Rappel bout-en-bout plafonné : "
            f"0.75 x {metrics['judge_recall']:.3f} = {metrics['end_to_end_recall']:.3f}."
        ),
        (
            f"Signal de viabilité v1 (non décisionnel) : "
            f"{'PASS' if gates['viability_signal']['pass'] else 'FAIL'} ; "
            "Julien conserve seul la décision go-production."
        ),
        "",
        "## Frontière de vérité terrain",
        "",
        "| Population | Total | Déclarées contradiction | Usage |",
        "|---|---:|---:|---|",
        (
            f"| Adjugées | {metrics['adjudicated_pairs']} "
            f"({metrics['positive_total']} positives, {metrics['negative_total']} négatives) | "
            f"{metrics['true_positives'] + metrics['false_positives']} | "
            "Précision et rappel réels |"
        ),
        (
            f"| Non jugées | {backlog['total']} | {backlog['declared_contradiction']} | "
            "Backlog d'adjudication uniquement |"
        ),
        "",
        "Aucune précision n'est inférée sur les 254 paires non jugées.",
        "",
        "## Rappel des positives adjugées par strate",
        "",
        "| Strate | Conservées | Rappel-juge |",
        "|---|---:|---:|",
    ]
    for item in evidence["positive_recall_by_stratum"]:
        lines.append(
            f"| {item['stratum']} | {item['retained']}/{item['total']} | {item['recall']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Erreurs sur les paires adjugées",
            "",
            "Faux positifs restants par portée prédite :",
        ]
    )
    if evidence["false_positive_analysis"]["by_judge_scope"]:
        for scope, count in evidence["false_positive_analysis"]["by_judge_scope"].items():
            lines.append(f"- `{scope}` : {count}.")
    else:
        lines.append("- Aucun faux positif.")
    lines.extend(["", "Faux négatifs :"])
    if evidence["false_negatives"]:
        for item in evidence["false_negatives"]:
            lines.append(
                f"- `{item['note_a']}` ↔ `{item['note_b']}` "
                f"({item['stratum']}, confiance {item['judgment']['confidence']:.3f}) : "
                f"{item['judgment']['rationale']}"
            )
    else:
        lines.append("- Aucun faux négatif.")
    frozen = evidence["frozen_input"]
    runtime = evidence["runtime"]
    lines.extend(
        [
            "",
            "## Entrée figée, cache et coût",
            "",
            f"- Évidence v2 SHA-256 : `{frozen['evidence_sha256']}`.",
            f"- Ranking canonique SHA-256 : `{frozen['ranking_sha256']}`.",
            f"- Modèle demandé : `{evidence['model']['model_id']}`.",
            f"- Version(s) modèle retournée(s) : {model_versions}.",
            f"- Prompt SHA-256 : `{evidence['model']['prompt_sha256']}`.",
            (
                f"- Température : {evidence['model']['temperature']}; seuil : "
                f"{evidence['model']['confidence_threshold']:.2f}."
            ),
            f"- Cache : {runtime['cache_entries']} entrées, SHA-256 `{runtime['cache_sha256']}`.",
            (
                f"- Génération initiale : {runtime['input_tokens']} tokens entrée, "
                f"{runtime['output_tokens']} sortie, latence cumulée "
                f"{runtime['generation_latency_seconds']:.3f} s, coût plafond estimé "
                f"{runtime['estimated_cost_upper_bound_usd']:.4f} USD."
            ),
            "",
            "## Déterminisme replayable",
            "",
        ]
    )
    for trial in evidence["determinism"]["trials"]:
        lines.append(
            f"- Replay {trial['trial']} : `{trial['ranking_sha256']}`, "
            f"{trial['retained_pairs']} paires, {trial['latency_seconds']:.3f} s, "
            f"cache misses = {trial['cache_misses']}."
        )
    anti_leak = evidence["anti_leak"]
    lines.extend(
        [
            "",
            "## Gardes",
            "",
            f"- Entrée figée : {'PASS' if gates['frozen_input']['pass'] else 'FAIL'}.",
            f"- Frontière 139/254 : {'PASS' if gates['population_boundary']['pass'] else 'FAIL'}.",
            f"- Déterminisme cache-only : {'PASS' if gates['determinism']['pass'] else 'FAIL'}.",
            (
                f"- Anti-fuite : {'PASS' if gates['anti_leak']['pass'] else 'FAIL'} "
                f"({anti_leak['few_shots_checked']} few-shot, "
                f"{anti_leak['pool_path_literals_checked']} chemins pool scannés)."
            ),
            (
                f"- Non-destruction rappel : {recall_status} "
                f"({metrics['true_positives']}/{metrics['positive_total']}, minimum "
                f"{gates['recall_preservation']['minimum_retained']})."
            ),
            "",
            "Le provider de mesure n'effectue aucun accès réseau pendant les trois replays : "
            "toute absence du cache provoque un STOP. Aucun changement `src/`, câblage MCP, "
            "gate wiki ou détecteur de production n'est inclus.",
            "",
            "Ce handoff doit être contre-vérifié depuis le cache, les labels et le ranking par "
            "le superviseur avant merge. Il ne constitue pas un go-production.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:  # noqa: PLR0915
    """Replay the complete cache three times and measure only adjudicated precision."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--detector", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--evidence-out", type=Path, required=True)
    parser.add_argument("--report-out", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    config = cast("dict[str, Any]", load_json(args.config))
    provider = load_detector(args.detector)
    frozen_evidence = cast("dict[str, Any]", provider.load_frozen_evidence(config))
    ranking = cast("list[dict[str, Any]]", frozen_evidence["ranking"])
    corpus_path = Path(str(config["corpus_path"]))
    labels_path = Path(str(config["labels_path"]))
    spec_path = Path(str(config["synthetic_spec_path"]))
    prompt_path = Path(str(config["prompt_path"]))
    cache_path = Path(str(config["cache_path"]))
    corpus = cast("list[dict[str, Any]]", load_json(corpus_path))
    labels = [
        cast("dict[str, Any]", json.loads(line))
        for line in labels_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    spec = cast("dict[str, Any]", load_json(spec_path))
    cache = cast("dict[str, Any]", provider.load_cache(config))
    notes = provider.load_corpus(corpus_path, config)

    observed_file_hash = sha256_file(Path(str(config["frozen_evidence_path"])))
    observed_ranking_hash = str(provider.frozen_ranking_hash(ranking))
    frozen_pass = (
        observed_file_hash == str(config["frozen_evidence_sha256"])
        and observed_ranking_hash == str(config["frozen_ranking_sha256"])
        and len(ranking) == int(config["expected_pool_pairs"])
    )
    anti_leak = anti_leak_findings(provider, config, corpus, ranking, labels)

    trials: list[dict[str, Any]] = []
    trial_candidates: list[list[Any]] = []
    for trial in range(1, int(config["trials"]) + 1):
        provider.begin_trial(trial)
        started = time.perf_counter()
        candidates = provider.score_pairs(notes, config, include_supersedes=True)
        latency = time.perf_counter() - started
        digest = digest_candidates(provider, candidates)
        metadata = cast("dict[str, Any]", provider.trial_metadata())
        trials.append(
            {
                "trial": trial,
                "ranking_sha256": digest,
                "provider_ranking_sha256": str(metadata["ranking_sha256"]),
                "retained_pairs": len(candidates),
                "cache_hits": int(metadata["cache_hits"]),
                "cache_misses": int(metadata["cache_misses"]),
                "cache_only": bool(metadata["cache_only"]),
                "latency_seconds": round(latency, 3),
            }
        )
        trial_candidates.append(candidates)
    candidates = trial_candidates[0]
    retained_keys = {pair_key(item.a.path, item.b.path) for item in candidates}
    ranking_keys = {pair_key(str(item["a"]), str(item["b"])) for item in ranking}
    labels_by_key = {pair_key(str(item["note_a"]), str(item["note_b"])): item for item in labels}
    adjudicated_keys = ranking_keys & set(labels_by_key)
    unjudged_keys = ranking_keys - set(labels_by_key)
    positives = {key for key in adjudicated_keys if int(labels_by_key[key]["label"]) == 1}
    negatives = adjudicated_keys - positives
    true_positives = positives & retained_keys
    false_negatives = positives - retained_keys
    false_positives = negatives & retained_keys
    true_negatives = negatives - retained_keys
    precision = len(true_positives) / (len(true_positives) + len(false_positives))
    judge_recall = len(true_positives) / len(positives)
    end_to_end_recall = float(config["generator_recall_ceiling"]) * judge_recall
    entries_by_pair = judgment_by_pair(cache)

    path_by_id = {str(item["id"]): str(item["path"]) for item in corpus}
    synthetic_strata = {
        pair_key(path_by_id[str(item["source_id"])], path_by_id[str(item["id"])]): str(
            item["stratum"]
        )
        for item in cast("list[dict[str, Any]]", spec["pairs"])
    }
    positive_stratum = {key: synthetic_strata.get(key, "curation") for key in positives}
    coverage = [
        coverage_record(
            labels_by_key[key],
            entries_by_pair[key],
            key in retained_keys,
            positive_stratum.get(key),
        )
        for key in sorted(adjudicated_keys)
    ]
    by_stratum: list[dict[str, Any]] = []
    for stratum in sorted(set(positive_stratum.values())):
        keys = {key for key, value in positive_stratum.items() if value == stratum}
        retained = len(keys & retained_keys)
        by_stratum.append(
            {
                "stratum": stratum,
                "retained": retained,
                "total": len(keys),
                "recall": retained / len(keys),
            }
        )
    false_negative_records = [
        next(item for item in coverage if pair_key(item["note_a"], item["note_b"]) == key)
        for key in sorted(false_negatives)
    ]
    false_positive_records = [
        next(item for item in coverage if pair_key(item["note_a"], item["note_b"]) == key)
        for key in sorted(false_positives)
    ]
    backlog_retained = sorted(unjudged_keys & retained_keys)

    cache_entries = list(cast("dict[str, dict[str, Any]]", cache["entries"]).values())
    input_tokens = sum(int(item["usage"]["input_tokens"]) for item in cache_entries)
    output_tokens = sum(int(item["usage"]["output_tokens"]) for item in cache_entries)
    generation_latencies = [float(item["latency_seconds"]) for item in cache_entries]
    pricing = cast("dict[str, float]", config["pricing_usd_per_million_tokens"])
    estimated_cost = (
        input_tokens * float(pricing["input"]) + output_tokens * float(pricing["output"])
    ) / 1_000_000
    model_versions = sorted({str(item["model"]) for item in cache_entries})
    deterministic = len({str(item["ranking_sha256"]) for item in trials}) == 1 and all(
        item["cache_only"] and item["cache_misses"] == 0 for item in trials
    )
    boundary_pass = (
        len(adjudicated_keys) == int(config["expected_adjudicated_pairs"])
        and len(positives) == int(config["expected_adjudicated_positives"])
        and len(negatives) == int(config["expected_adjudicated_negatives"])
        and len(unjudged_keys) == int(config["expected_unjudged_pairs"])
    )
    recall_pass = len(true_positives) >= int(config["minimum_retained_positives"])
    viability_pass = precision >= float(config["viability_precision_threshold"])
    measurement_valid = all(
        (frozen_pass, boundary_pass, deterministic, bool(anti_leak["pass"]), recall_pass)
    )

    evidence: dict[str, Any] = {
        "schema_version": 1,
        "measurement": "llm-judge-on-frozen-ranking",
        "measurement_date": "2026-07-13",
        "production_decision": None,
        "provider": str(provider.PROVIDER_NAME),
        "provider_version": str(config["provider_version"]),
        "model": {
            "model_id": str(config["model_id"]),
            "model_version": str(config["model_version"]),
            "observed_versions": model_versions,
            "api_backend": str(config["api_backend"]),
            "api_endpoint": str(config["api_endpoint"]),
            "temperature": float(config["temperature"]),
            "seed": int(config["seed"]),
            "prompt_sha256": str(provider.prompt_sha256(config)),
            "confidence_threshold": float(config["confidence_threshold"]),
            "context_selection": str(config["context_selection"]),
        },
        "frozen_input": {
            "evidence_path": str(config["frozen_evidence_path"]),
            "evidence_sha256": observed_file_hash,
            "expected_evidence_sha256": str(config["frozen_evidence_sha256"]),
            "ranking_sha256": observed_ranking_hash,
            "expected_ranking_sha256": str(config["frozen_ranking_sha256"]),
            "pairs": len(ranking),
            "regenerated": False,
        },
        "inputs": {
            "corpus": {"path": corpus_path.as_posix(), "sha256": sha256_file(corpus_path)},
            "labels": {"path": labels_path.as_posix(), "sha256": sha256_file(labels_path)},
            "synthetic_spec": {"path": spec_path.as_posix(), "sha256": sha256_file(spec_path)},
            "prompt": {"path": prompt_path.as_posix(), "sha256": sha256_file(prompt_path)},
            "config": {"path": args.config.as_posix(), "sha256": sha256_file(args.config)},
            "cache": {"path": cache_path.as_posix(), "sha256": sha256_file(cache_path)},
            "provider_module": {
                "path": args.detector.as_posix(),
                "sha256": sha256_file(args.detector),
            },
        },
        "population": {
            "pool_pairs": len(ranking_keys),
            "adjudicated_pairs": len(adjudicated_keys),
            "adjudicated_positives": len(positives),
            "adjudicated_negatives": len(negatives),
            "unjudged_pairs": len(unjudged_keys),
        },
        "adjudicated_metrics": {
            "adjudicated_pairs": len(adjudicated_keys),
            "positive_total": len(positives),
            "negative_total": len(negatives),
            "true_positives": len(true_positives),
            "false_positives": len(false_positives),
            "true_negatives": len(true_negatives),
            "false_negatives": len(false_negatives),
            "precision": precision,
            "judge_recall": judge_recall,
            "generator_recall_ceiling": float(config["generator_recall_ceiling"]),
            "end_to_end_recall": end_to_end_recall,
            "keep_all_precision": len(positives) / len(adjudicated_keys),
            "precision_lift_vs_keep_all": precision / (len(positives) / len(adjudicated_keys)),
        },
        "positive_recall_by_stratum": by_stratum,
        "unjudged_backlog": {
            "total": len(unjudged_keys),
            "declared_contradiction": len(backlog_retained),
            "not_declared_contradiction": len(unjudged_keys) - len(backlog_retained),
            "precision_inferred": False,
            "retained_pairs": [{"note_a": key[0], "note_b": key[1]} for key in backlog_retained],
        },
        "false_positive_analysis": {
            "total": len(false_positive_records),
            "by_judge_scope": dict(
                sorted(
                    Counter(item["judgment"]["scope"] for item in false_positive_records).items()
                )
            ),
            "pairs": false_positive_records,
        },
        "false_negatives": false_negative_records,
        "adjudicated_coverage": coverage,
        "runtime": {
            "cache_entries": len(cache_entries),
            "cache_sha256": sha256_file(cache_path),
            "api_calls_recorded": sum(int(item["attempts"]) for item in cache_entries),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "generation_latency_seconds": round(sum(generation_latencies), 3),
            "generation_latency_median_seconds": round(statistics.median(generation_latencies), 3),
            "estimated_cost_upper_bound_usd": round(estimated_cost, 6),
            "cost_ignores_any_provider_cache_discount": True,
        },
        "determinism": {"identical": deterministic, "trials": trials},
        "anti_leak": anti_leak,
        "gates": {
            "frozen_input": {"pass": frozen_pass},
            "population_boundary": {"pass": boundary_pass},
            "determinism": {"pass": deterministic},
            "anti_leak": {"pass": bool(anti_leak["pass"])},
            "recall_preservation": {
                "operator": ">=",
                "minimum_retained": int(config["minimum_retained_positives"]),
                "observed_retained": len(true_positives),
                "pass": recall_pass,
            },
            "viability_signal": {
                "decision_authority": "Julien",
                "decision_made": False,
                "operator": ">=",
                "precision_threshold": float(config["viability_precision_threshold"]),
                "observed_precision": precision,
                "pass": viability_pass,
            },
            "measurement_valid": measurement_valid,
        },
        "retained_ranking": [
            {"output_rank": rank, **provider.candidate_payload(candidate)}
            for rank, candidate in enumerate(candidates, 1)
        ],
    }
    with args.evidence_out.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    with args.report_out.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(render_report(evidence))
    LOGGER.info("Evidence written to %s", args.evidence_out)
    LOGGER.info("Report written to %s", args.report_out)
    LOGGER.info(
        "Adjudicated precision=%.3f recall=%.3f TP=%d FP=%d; backlog=%d; valid=%s",
        precision,
        judge_recall,
        len(true_positives),
        len(false_positives),
        len(backlog_retained),
        measurement_valid,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
