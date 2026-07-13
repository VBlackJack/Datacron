# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Julien Bombled
"""Measure recall and pool budget for the deterministic claim generator."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, cast

from run_measure import digest_candidates, load_detector, merge_config, pair_key

LOGGER = logging.getLogger("datacron.contradiction.claim_decomp_measure")


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a fixed input or versioned rule file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> Any:
    """Load a UTF-8 JSON document."""
    return json.loads(path.read_text(encoding="utf-8"))


def positive_record(
    key: tuple[str, str],
    source: str,
    stratum: str | None,
    rank_by_key: dict[tuple[str, str], int],
) -> dict[str, Any]:
    """Describe reachability of one positive in the generated ranking."""
    return {
        "note_a": key[0],
        "note_b": key[1],
        "label_source": source,
        "stratum": stratum,
        "present": key in rank_by_key,
        "rank": rank_by_key.get(key),
    }


def stratum_metrics(coverage: list[dict[str, Any]], cutoff: int | None) -> list[dict[str, Any]]:
    """Return synthetic recall by stratum at a cutoff or at the full pool."""
    synthetic = [item for item in coverage if item["label_source"] == "synthetique"]
    result: list[dict[str, Any]] = []
    for stratum in sorted({str(item["stratum"]) for item in synthetic}):
        items = [item for item in synthetic if item["stratum"] == stratum]
        present = sum(
            item["rank"] is not None and (cutoff is None or int(item["rank"]) <= cutoff)
            for item in items
        )
        result.append(
            {
                "stratum": stratum,
                "present": present,
                "total": len(items),
                "recall": present / len(items),
            }
        )
    return result


def render_report(evidence: dict[str, Any]) -> str:
    """Render a French measurement-first report with both fail-fast gates."""
    recall_gate = evidence["gates"]["recall"]
    budget_gate = evidence["gates"]["budget"]
    pool = evidence["pool"]
    status = "PASS — LOT 10 éligible" if evidence["gates"]["lot10_eligible"] else "STOP"
    lines = [
        "# Mesure du générateur par décomposition en claims",
        "",
        "Date : 2026-07-13",
        f"Statut : {status}",
        "",
        "## Verdict",
        "",
        (
            f"Le pool plein récupère {recall_gate['present']}/{recall_gate['total']} "
            f"positifs synthétiques (rappel {recall_gate['observed_recall']:.2f}) contre "
            f"le plafond LOT 8 de {recall_gate['baseline_recall']:.2f}."
        ),
        (
            f"Le générateur émet {pool['candidate_pairs']} paires uniques, soit "
            f"{pool['ratio_of_all_pairs']:.2%} des {pool['all_possible_pairs']} paires "
            f"possibles, pour un budget maximal de {budget_gate['max_pool_pairs']}."
        ),
        "",
        (
            f"Garde rappel : {'PASS' if recall_gate['pass'] else 'FAIL'} ; "
            f"garde budget : {'PASS' if budget_gate['pass'] else 'FAIL'}."
        ),
        "",
        "## Rappel et taille du ranking",
        "",
        "| Cutoff | Candidats examinés | Positifs synthétiques | Rappel | Positifs curation |",
        "|---:|---:|---:|---:|---:|",
    ]
    for point in evidence["recall_at_cutoffs"]:
        lines.append(
            f"| {point['cutoff']} | {point['retrieved']} | "
            f"{point['synthetic_present']}/{point['synthetic_total']} | "
            f"{point['synthetic_recall']:.2f} | "
            f"{point['curation_present']}/{point['curation_total']} |"
        )
    full = evidence["full_pool_recall"]
    lines.append(
        f"| pool plein | {pool['candidate_pairs']} | "
        f"{full['synthetic_present']}/{full['synthetic_total']} | "
        f"{full['synthetic_recall']:.2f} | "
        f"{full['curation_present']}/{full['curation_total']} |"
    )
    lines.extend(
        [
            "",
            "## Rappel synthétique par strate, pool plein",
            "",
            "| Strate | Présents | Rappel |",
            "|---|---:|---:|",
        ]
    )
    for item in evidence["recall_by_stratum"]["full_pool"]:
        lines.append(
            f"| {item['stratum']} | {item['present']}/{item['total']} | {item['recall']:.2f} |"
        )
    canary = evidence["canary"]
    lines.extend(
        [
            "",
            "## Canary naturelle",
            "",
            f"Canary {'présente' if canary['present'] else 'absente'} du pool"
            + (f" au rang {canary['rank']}." if canary["present"] else "."),
            "Elle reste séparée de toutes les métriques synthétiques.",
            "",
            "## Méthode",
            "",
            "Le provider découpe les titres et corps du corpus figé en claims atomiques. "
            "Les alias, marqueurs, regex de valeur et seuils vivent dans les fichiers de "
            "règles/configuration. Les sujets compatibles sont rapprochés avec des termes "
            "rares pondérés par IDF ; une paire n'est émise que sur conflit potentiel de "
            "polarité ou de valeur/état. Aucun LLM, réseau ou accès au vault n'est utilisé.",
            "",
            "Le budget est mesuré sur la sortie pleine non tronquée. Le ranking complet est "
            "inclus dans l'évidence afin de permettre un recalcul indépendant.",
            "",
            "## Déterminisme et intégrité",
            "",
            f"Déterminisme : {'PASS' if evidence['determinism']['identical'] else 'FAIL'}.",
        ]
    )
    for trial in evidence["determinism"]["trials"]:
        lines.append(
            f"- Run {trial['trial']} : ranking SHA-256 `{trial['ranking_sha256']}`, "
            f"{trial['latency_seconds']:.3f} s."
        )
    inputs = evidence["inputs"]
    lines.extend(
        [
            "",
            f"- `corpus.json` SHA-256 : `{inputs['corpus']['sha256']}`.",
            f"- `labels.jsonl` SHA-256 : `{inputs['labels']['sha256']}`.",
            f"- `synthetic-spec.json` SHA-256 : `{inputs['synthetic_spec']['sha256']}`.",
            f"- Règles SHA-256 : `{inputs['rules']['sha256']}`.",
            "",
            "Le fichier de labels courant contient 274 paires ; il est réutilisé tel quel, "
            "sans réétiquetage. `model_id` et `prompt_sha256` sont nuls car l'extraction "
            "est entièrement fondée sur des règles locales.",
            "",
            "## Fork à trancher par Julien",
            "",
        ]
    )
    if evidence["gates"]["lot10_eligible"]:
        lines.append(
            "Les deux gardes sont franchies : un LOT 10 peut mesurer un juge sur ce pool, "
            "sans que ce handoff constitue une décision de production. Une contre-vérification "
            "superviseur doit recalculer rappel et budget et muter les règles pour vérifier "
            "la réaction des gardes avant merge."
        )
    elif not recall_gate["pass"]:
        lines.append(
            "Le rappel ne dépasse pas le LOT 8 : parquer ou changer de générateur. Ne pas "
            "ajouter de juge à un pool qui ne contient toujours pas les positifs."
        )
    else:
        lines.append(
            "Le rappel progresse mais le pool dépasse le budget : resserrer l'appariement "
            "des claims et remesurer. Ne pas masquer l'explosion par un cutoff ni ajouter "
            "un juge sur cette sortie inexploitable."
        )
    lines.extend(
        [
            "",
            "Aucun détecteur de production, juge LLM, câblage MCP ou changement `src/` "
            "n'est inclus dans ce lot.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:  # noqa: PLR0915
    """Run three full deterministic trials and write self-contained evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--detector", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--evidence-out", type=Path, required=True)
    parser.add_argument("--report-out", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    provider = load_detector(args.detector)
    config = merge_config(provider.DEFAULT_CONFIG, args.config)
    corpus_path = Path(str(config["corpus_path"]))
    labels_path = Path(str(config["labels_path"]))
    spec_path = Path(str(config["synthetic_spec_path"]))
    rules_path = Path(str(config["rules_path"]))
    corpus = cast("list[dict[str, Any]]", load_json(corpus_path))
    labels = [
        cast("dict[str, Any]", json.loads(line))
        for line in labels_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    spec = cast("dict[str, Any]", load_json(spec_path))
    notes = provider.load_corpus(corpus_path, config)

    trial_results: list[dict[str, Any]] = []
    rankings: list[list[Any]] = []
    for trial in range(1, int(config["trials"]) + 1):
        provider.begin_trial(trial)
        started = time.perf_counter()
        candidates = provider.score_pairs(notes, config, include_supersedes=True)
        elapsed = time.perf_counter() - started
        digest = digest_candidates(provider, candidates)
        metadata = cast("dict[str, Any]", provider.trial_metadata())
        trial_results.append(
            {
                "trial": trial,
                "ranking_sha256": digest,
                "provider_ranking_sha256": metadata["ranking_sha256"],
                "claim_extraction_sha256": metadata["claims_by_note_sha256"],
                "latency_seconds": round(elapsed, 3),
                "claim_count": metadata["claim_count"],
                "pool_pairs": len(candidates),
            }
        )
        rankings.append(candidates)

    candidates = rankings[0]
    rank_by_key = {
        pair_key(candidate.a.path, candidate.b.path): rank
        for rank, candidate in enumerate(candidates, 1)
    }
    path_by_id = {str(item["id"]): str(item["path"]) for item in corpus}
    synthetic_entries: list[tuple[tuple[str, str], str]] = []
    for item in cast("list[dict[str, Any]]", spec["pairs"]):
        key = pair_key(path_by_id[str(item["source_id"])], path_by_id[str(item["id"])])
        synthetic_entries.append((key, str(item["stratum"])))
    curation_labels = [
        item for item in labels if int(item["label"]) == 1 and item["source"] == "curation"
    ]
    coverage = [
        positive_record(key, "synthetique", stratum, rank_by_key)
        for key, stratum in synthetic_entries
    ]
    coverage.extend(
        positive_record(
            pair_key(str(item["note_a"]), str(item["note_b"])),
            "curation",
            None,
            rank_by_key,
        )
        for item in curation_labels
    )

    cutoff_metrics: list[dict[str, Any]] = []
    for cutoff_value in cast("list[int]", config["cutoffs"]):
        cutoff = int(cutoff_value)
        synthetic_present = sum(
            item["label_source"] == "synthetique"
            and item["rank"] is not None
            and int(item["rank"]) <= cutoff
            for item in coverage
        )
        curation_present = sum(
            item["label_source"] == "curation"
            and item["rank"] is not None
            and int(item["rank"]) <= cutoff
            for item in coverage
        )
        cutoff_metrics.append(
            {
                "cutoff": cutoff,
                "retrieved": min(cutoff, len(candidates)),
                "synthetic_present": synthetic_present,
                "synthetic_total": len(synthetic_entries),
                "synthetic_recall": synthetic_present / len(synthetic_entries),
                "curation_present": curation_present,
                "curation_total": len(curation_labels),
            }
        )

    synthetic_present_full = sum(
        item["label_source"] == "synthetique" and item["present"] for item in coverage
    )
    curation_present_full = sum(
        item["label_source"] == "curation" and item["present"] for item in coverage
    )
    all_possible_pairs = len(notes) * (len(notes) - 1) // 2
    baseline_recall = float(config["baseline_full_pool_recall"])
    max_pool_pairs = int(config["max_pool_pairs"])
    full_recall = synthetic_present_full / len(synthetic_entries)
    recall_pass = full_recall > baseline_recall
    budget_pass = len(candidates) <= max_pool_pairs
    canary = cast("dict[str, Any]", spec["canary"])
    canary_key = pair_key(str(canary["note_a"]), str(canary["note_b"]))
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "measurement": "claim-decomposition-candidate-generation",
        "measurement_date": "2026-07-13",
        "provider": str(getattr(provider, "PROVIDER_NAME", args.detector.stem)),
        "model_id": None,
        "prompt_sha256": None,
        "inputs": {
            "corpus": {
                "path": corpus_path.as_posix(),
                "sha256": sha256_file(corpus_path),
                "notes": len(corpus),
            },
            "labels": {
                "path": labels_path.as_posix(),
                "sha256": sha256_file(labels_path),
                "pairs": len(labels),
            },
            "synthetic_spec": {
                "path": spec_path.as_posix(),
                "sha256": sha256_file(spec_path),
            },
            "config": {
                "path": args.config.as_posix(),
                "sha256": sha256_file(args.config),
            },
            "rules": {
                "path": rules_path.as_posix(),
                "sha256": sha256_file(rules_path),
            },
            "provider_module": {
                "path": args.detector.as_posix(),
                "sha256": sha256_file(args.detector),
            },
        },
        "pool": {
            "candidate_pairs": len(candidates),
            "all_possible_pairs": all_possible_pairs,
            "ratio_of_all_pairs": len(candidates) / all_possible_pairs,
            "is_all_pairs": len(candidates) == all_possible_pairs,
        },
        "recall_at_cutoffs": cutoff_metrics,
        "full_pool_recall": {
            "synthetic_present": synthetic_present_full,
            "synthetic_total": len(synthetic_entries),
            "synthetic_recall": full_recall,
            "curation_present": curation_present_full,
            "curation_total": len(curation_labels),
            "curation_recall": curation_present_full / len(curation_labels),
        },
        "recall_by_stratum": {
            "cutoffs": {
                str(cutoff): stratum_metrics(coverage, cutoff)
                for cutoff in cast("list[int]", config["cutoffs"])
            },
            "full_pool": stratum_metrics(coverage, None),
        },
        "canary": {
            "note_a": canary_key[0],
            "note_b": canary_key[1],
            "present": canary_key in rank_by_key,
            "rank": rank_by_key.get(canary_key),
            "included_in_synthetic_metrics": False,
        },
        "determinism": {
            "identical": len({item["ranking_sha256"] for item in trial_results}) == 1,
            "trials": trial_results,
        },
        "gates": {
            "recall": {
                "operator": ">",
                "baseline_recall": baseline_recall,
                "observed_recall": full_recall,
                "present": synthetic_present_full,
                "total": len(synthetic_entries),
                "pass": recall_pass,
            },
            "budget": {
                "operator": "<=",
                "max_pool_pairs": max_pool_pairs,
                "observed_pool_pairs": len(candidates),
                "pass": budget_pass,
            },
            "lot10_eligible": recall_pass and budget_pass,
        },
        "positive_coverage": coverage,
        "ranking": [
            {
                "rank": rank,
                "a": candidate.a.path,
                "b": candidate.b.path,
                "score": round(float(candidate.score), 6),
                "shared_subjects": candidate.shared_subjects,
                "conflict_types": candidate.conflict_types,
            }
            for rank, candidate in enumerate(candidates, 1)
        ],
        "claim_samples": [
            {
                "rank": rank,
                "a": candidate.a.path,
                "b": candidate.b.path,
                "best_claim_match": candidate.claim_matches[0],
            }
            for rank, candidate in enumerate(
                candidates[: int(config["evidence_claim_sample_size"])], 1
            )
        ],
    }
    args.evidence_out.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.report_out.write_text(render_report(evidence), encoding="utf-8")
    LOGGER.info("Evidence written to %s", args.evidence_out)
    LOGGER.info("Report written to %s", args.report_out)
    LOGGER.info(
        "Gates: recall=%.3f pass=%s pool=%d/%d pass=%s",
        full_recall,
        recall_pass,
        len(candidates),
        max_pool_pairs,
        budget_pass,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
