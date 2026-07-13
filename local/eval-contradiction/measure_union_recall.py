# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Julien Bombled
"""Measure the recall ceiling of persisted lexical and semantic candidate pools."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, cast

from run_measure import pair_key

LOGGER = logging.getLogger("datacron.contradiction.union_recall")


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file without changing it."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    """Load one UTF-8 JSON document."""
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_input(config_path: Path, value: object) -> Path:
    """Resolve a configured input relative to the configuration file."""
    path = Path(str(value))
    return path if path.is_absolute() else config_path.parent / path


def candidate_key(candidate: dict[str, Any]) -> tuple[str, str]:
    """Return the harness pair key for a persisted candidate."""
    return pair_key(str(candidate["a"]), str(candidate["b"]))


def merge_ranked_pools(
    pools: list[tuple[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Round-robin provider ranks and retain each order-independent pair once."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    max_length = max((len(items) for _, items in pools), default=0)
    for source_rank in range(1, max_length + 1):
        for provider, items in pools:
            if source_rank > len(items):
                continue
            candidate = items[source_rank - 1]
            key = candidate_key(candidate)
            existing = by_key.get(key)
            if existing is not None:
                existing["provider_ranks"][provider] = source_rank
                continue
            item: dict[str, Any] = {
                "note_a": key[0],
                "note_b": key[1],
                "first_provider": provider,
                "provider_ranks": {provider: source_rank},
            }
            by_key[key] = item
            ordered.append(item)
    for union_rank, item in enumerate(ordered, 1):
        item["union_rank"] = union_rank
    return ordered


def positive_record(
    label: dict[str, Any],
    union_by_key: dict[tuple[str, str], dict[str, Any]],
    stratum_by_key: dict[tuple[str, str], str],
) -> dict[str, Any]:
    """Describe whether one adjudicated positive is reachable by the union."""
    key = pair_key(str(label["note_a"]), str(label["note_b"]))
    candidate = union_by_key.get(key)
    return {
        "note_a": key[0],
        "note_b": key[1],
        "label_source": str(label["source"]),
        "stratum": stratum_by_key.get(key),
        "present": candidate is not None,
        "union_rank": candidate["union_rank"] if candidate is not None else None,
        "provider_ranks": candidate["provider_ranks"] if candidate is not None else {},
    }


def render_report(evidence: dict[str, Any]) -> str:
    """Render the fail-fast result in the established measurement-report style."""
    gate = evidence["fail_fast_gate"]
    ceiling = evidence["ceiling"]
    inputs = evidence["inputs"]
    verdict = "STOP — garde fail-fast déclenchée" if gate["stop"] else "CONTINUE"
    lines = [
        "# Plafond de rappel de l'union des candidats",
        "",
        "Date : 2026-07-13",
        f"Statut : {verdict}",
        "",
        "## Verdict",
        "",
        (
            f"Le rappel synthétique de l'union au cutoff 100 est "
            f"{gate['observed_recall']:.2f} ({gate['present']}/{gate['total']}). "
            f"La garde impose l'arrêt pour une valeur ≤ {gate['threshold']:.2f} : "
            "T1/T2 ne doivent pas être implémentés."
        ),
        "",
        (
            f"Même à épuisement de l'union, seuls {ceiling['synthetic_present']}/"
            f"{ceiling['synthetic_total']} positifs synthétiques sont candidats "
            f"(plafond {ceiling['synthetic_recall']:.2f}). Un juge ne peut pas récupérer "
            f"les {ceiling['synthetic_absent']} contradictions absentes."
        ),
        "",
        "## Chiffres",
        "",
        "| Cutoff demandé | Candidats présents | Positifs synthétiques | "
        "Rappel synthétique | Positifs de curation |",
        "|---:|---:|---:|---:|---:|",
    ]
    for point in evidence["recall_at_cutoffs"]:
        lines.append(
            f"| {point['cutoff']} | {point['retrieved']} | "
            f"{point['synthetic_present']}/{point['synthetic_total']} | "
            f"{point['synthetic_recall']:.2f} | "
            f"{point['curation_present']}/{point['curation_total']} |"
        )
    lines.extend(
        [
            "",
            "## Plafond explicite",
            "",
            f"- Union unique : {evidence['union']['unique_pairs']} paires "
            f"({evidence['union']['duplicate_pairs']} doublons inter-provider supprimés).",
            f"- Positifs synthétiques présents : {ceiling['synthetic_present']}/"
            f"{ceiling['synthetic_total']} ; absents : {ceiling['synthetic_absent']}.",
            f"- Tous positifs adjugés présents : {ceiling['adjudicated_present']}/"
            f"{ceiling['adjudicated_total']} ; absents : {ceiling['adjudicated_absent']}.",
            "- Canary naturelle : absente de l'union et tenue séparée du rappel synthétique.",
            "",
            "## Méthode",
            "",
            "Les deux rankings persistés `lexical top-100` et `semantic-cortex top-100` "
            "sont relus sans appel modèle et sans accès au vault. Le calcul réutilise "
            "la clé de paire indépendante de l'ordre de `run_measure.py`. Comme les scores "
            "des providers ne sont pas commensurables, les rangs sont entrelacés de façon "
            "stable (lexical puis sémantique à chaque rang source), puis dédupliqués. Les "
            "cutoffs portent sur ce ranking d'union.",
            "",
            "`corpus.json` et `labels.jsonl` ont été relus tels quels, sans régénération ni "
            "réétiquetage. Le fichier de labels courant contient 274 paires, malgré les 117 "
            "annoncées dans le contexte de mission ; la mesure journalise l'état réellement "
            "versionné.",
            "",
            "## Intégrité des entrées",
            "",
            f"- `corpus.json` SHA-256 : `{inputs['corpus']['sha256']}`.",
            f"- `labels.jsonl` SHA-256 : `{inputs['labels']['sha256']}`.",
        ]
    )
    for provider in evidence["providers"]:
        lines.append(
            f"- `{provider['artifact']}` ({provider['provider']}) SHA-256 : `{provider['sha256']}`."
        )
    lines.extend(
        [
            "",
            "Toutes les 162 paires de l'union possèdent une adjudication dans le fichier "
            "de labels. `model_id` et `prompt_sha256` sont explicitement nuls : T0 n'exécute "
            "aucun juge et n'utilise aucun prompt.",
            "",
            "## Recommandation : parquer / changer d'approche (décomposition en claims)",
            "",
            "Parquer le fork LLM-juge sur ce pool. À cutoff 100, il ne pourrait juger que "
            "3 des 20 contradictions synthétiques ; même avec une classification parfaite, "
            "son rappel resterait à 0,15. À épuisement des 162 paires uniques, le plafond ne "
            "monte qu'à 0,20.",
            "",
            "Le prochain fork devrait agir sur la génération, par exemple en décomposant "
            "chaque note en claims atomiques (sujet, portée, polarité, valeur/état et contexte "
            "temporel), puis en appariant les claims compatibles avant jugement. Il faut "
            "remesurer le plafond de rappel de ce nouveau pool avant tout investissement dans "
            "un juge génératif.",
            "",
            "STOP : aucun provider LLM-juge, prompt, cache ni détecteur de production n'est "
            "implémenté dans ce lot.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:  # noqa: PLR0915
    """Measure candidate-union recall and write deterministic T0 artifacts."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--evidence-out", type=Path, required=True)
    parser.add_argument("--report-out", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    config = cast("dict[str, Any]", load_json(args.config))
    corpus_path = resolve_input(args.config, config["corpus"])
    labels_path = resolve_input(args.config, config["labels"])
    spec_path = resolve_input(args.config, config["synthetic_spec"])
    corpus = cast("list[dict[str, Any]]", load_json(corpus_path))
    labels = [
        cast("dict[str, Any]", json.loads(line))
        for line in labels_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    spec = cast("dict[str, Any]", load_json(spec_path))

    pools: list[tuple[str, list[dict[str, Any]]]] = []
    providers: list[dict[str, Any]] = []
    for provider_config in cast("list[dict[str, Any]]", config["providers"]):
        artifact_path = resolve_input(args.config, provider_config["artifact"])
        candidates = cast("list[dict[str, Any]]", load_json(artifact_path))
        expected = int(provider_config["top_n"])
        if len(candidates) != expected:
            raise ValueError(
                f"{artifact_path} contains {len(candidates)} candidates, expected {expected}"
            )
        provider_name = str(provider_config["provider"])
        pools.append((provider_name, candidates))
        providers.append(
            {
                "provider": provider_name,
                "artifact": artifact_path.name,
                "candidate_count": len(candidates),
                "sha256": sha256_file(artifact_path),
                "model_id": provider_config.get("model_id"),
            }
        )

    union = merge_ranked_pools(pools)
    union_by_key = {pair_key(str(item["note_a"]), str(item["note_b"])): item for item in union}
    labels_by_key = {pair_key(str(item["note_a"]), str(item["note_b"])): item for item in labels}
    unlabelled = [key for key in union_by_key if key not in labels_by_key]
    if unlabelled:
        raise ValueError(f"Union contains {len(unlabelled)} unlabelled pairs")

    path_by_id = {str(item["id"]): str(item["path"]) for item in corpus}
    stratum_by_key = {
        pair_key(path_by_id[str(item["source_id"])], path_by_id[str(item["id"])]): str(
            item["stratum"]
        )
        for item in cast("list[dict[str, Any]]", spec["pairs"])
    }
    positives = [item for item in labels if int(item["label"]) == 1]
    synthetic = [item for item in positives if item["source"] == "synthetique"]
    curation = [item for item in positives if item["source"] == "curation"]
    positive_coverage = [positive_record(item, union_by_key, stratum_by_key) for item in positives]

    cutoff_metrics: list[dict[str, Any]] = []
    for cutoff_value in cast("list[int]", config["cutoffs"]):
        cutoff = int(cutoff_value)
        keys = {pair_key(str(item["note_a"]), str(item["note_b"])) for item in union[:cutoff]}
        synthetic_present = sum(
            pair_key(str(item["note_a"]), str(item["note_b"])) in keys for item in synthetic
        )
        curation_present = sum(
            pair_key(str(item["note_a"]), str(item["note_b"])) in keys for item in curation
        )
        cutoff_metrics.append(
            {
                "cutoff": cutoff,
                "retrieved": min(cutoff, len(union)),
                "synthetic_present": synthetic_present,
                "synthetic_total": len(synthetic),
                "synthetic_recall": synthetic_present / len(synthetic),
                "curation_present": curation_present,
                "curation_total": len(curation),
                "adjudicated_positive_present": synthetic_present + curation_present,
                "adjudicated_positive_total": len(positives),
                "adjudicated_positive_recall": (synthetic_present + curation_present)
                / len(positives),
            }
        )

    gate_cutoff = int(config["fail_fast"]["cutoff"])
    gate_metric = next(item for item in cutoff_metrics if item["cutoff"] == gate_cutoff)
    gate_threshold = float(config["fail_fast"]["max_recall_to_stop"])
    synthetic_present_total = sum(
        item["present"] for item in positive_coverage if item["label_source"] == "synthetique"
    )
    adjudicated_present_total = sum(item["present"] for item in positive_coverage)
    canary = cast("dict[str, Any]", spec["canary"])
    canary_key = pair_key(str(canary["note_a"]), str(canary["note_b"]))
    gate_stop = bool(gate_metric["synthetic_recall"] <= gate_threshold)
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "measurement": "candidate-union-recall-ceiling",
        "measurement_date": "2026-07-13",
        "model_id": None,
        "prompt_sha256": None,
        "inputs": {
            "corpus": {
                "path": corpus_path.name,
                "sha256": sha256_file(corpus_path),
                "notes": len(corpus),
            },
            "labels": {
                "path": labels_path.name,
                "sha256": sha256_file(labels_path),
                "pairs": len(labels),
                "positive": len(positives),
                "negative": len(labels) - len(positives),
                "synthetic_positive": len(synthetic),
                "curation_positive": len(curation),
            },
            "synthetic_spec": {
                "path": spec_path.name,
                "sha256": sha256_file(spec_path),
            },
        },
        "providers": providers,
        "union": {
            "strategy": "stable-round-robin-provider-rank-lexical-first-deduplicated",
            "raw_pairs": sum(len(items) for _, items in pools),
            "unique_pairs": len(union),
            "duplicate_pairs": sum(len(items) for _, items in pools) - len(union),
            "all_pairs_labelled": not unlabelled,
            "ranking_sha256": hashlib.sha256(
                json.dumps(union, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        },
        "recall_at_cutoffs": cutoff_metrics,
        "ceiling": {
            "synthetic_present": synthetic_present_total,
            "synthetic_absent": len(synthetic) - synthetic_present_total,
            "synthetic_total": len(synthetic),
            "synthetic_recall": synthetic_present_total / len(synthetic),
            "adjudicated_present": adjudicated_present_total,
            "adjudicated_absent": len(positives) - adjudicated_present_total,
            "adjudicated_total": len(positives),
            "adjudicated_recall": adjudicated_present_total / len(positives),
        },
        "canary": {
            "note_a": canary_key[0],
            "note_b": canary_key[1],
            "present": canary_key in union_by_key,
            "included_in_synthetic_metrics": False,
        },
        "positive_coverage": positive_coverage,
        "fail_fast_gate": {
            "cutoff": gate_cutoff,
            "threshold": gate_threshold,
            "operator": "<=",
            "observed_recall": gate_metric["synthetic_recall"],
            "present": gate_metric["synthetic_present"],
            "total": gate_metric["synthetic_total"],
            "stop": gate_stop,
            "t1_t2_implemented": False,
        },
    }
    args.evidence_out.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.report_out.write_text(render_report(evidence), encoding="utf-8")
    LOGGER.info("Evidence written to %s", args.evidence_out)
    LOGGER.info("Report written to %s", args.report_out)
    LOGGER.info(
        "Fail-fast gate: recall@%d=%.3f threshold=%.3f stop=%s",
        gate_cutoff,
        gate_metric["synthetic_recall"],
        gate_threshold,
        gate_stop,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
