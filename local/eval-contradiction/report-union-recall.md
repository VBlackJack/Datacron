# Plafond de rappel de l'union des candidats

Date : 2026-07-13
Statut : STOP — garde fail-fast déclenchée

## Verdict

Le rappel synthétique de l'union au cutoff 100 est 0.15 (3/20). La garde impose l'arrêt pour une valeur ≤ 0.15 : T1/T2 ne doivent pas être implémentés.

Même à épuisement de l'union, seuls 4/20 positifs synthétiques sont candidats (plafond 0.20). Un juge ne peut pas récupérer les 16 contradictions absentes.

## Chiffres

| Cutoff demandé | Candidats présents | Positifs synthétiques | Rappel synthétique | Positifs de curation |
|---:|---:|---:|---:|---:|
| 25 | 25 | 2/20 | 0.10 | 0/4 |
| 50 | 50 | 3/20 | 0.15 | 0/4 |
| 100 | 100 | 3/20 | 0.15 | 0/4 |
| 200 | 162 | 4/20 | 0.20 | 0/4 |

## Plafond explicite

- Union unique : 162 paires (38 doublons inter-provider supprimés).
- Positifs synthétiques présents : 4/20 ; absents : 16.
- Tous positifs adjugés présents : 4/24 ; absents : 20.
- Canary naturelle : absente de l'union et tenue séparée du rappel synthétique.

## Méthode

Les deux rankings persistés `lexical top-100` et `semantic-cortex top-100` sont relus sans appel modèle et sans accès au vault. Le calcul réutilise la clé de paire indépendante de l'ordre de `run_measure.py`. Comme les scores des providers ne sont pas commensurables, les rangs sont entrelacés de façon stable (lexical puis sémantique à chaque rang source), puis dédupliqués. Les cutoffs portent sur ce ranking d'union.

`corpus.json` et `labels.jsonl` ont été relus tels quels, sans régénération ni réétiquetage. Le fichier de labels courant contient 274 paires, malgré les 117 annoncées dans le contexte de mission ; la mesure journalise l'état réellement versionné.

## Intégrité des entrées

- `corpus.json` SHA-256 : `c1dbfee8d4f1df41c34a9e06cd5d1eab9d3619816e2106e8d29e71de90fd1b42`.
- `labels.jsonl` SHA-256 : `28c0a51aa2b72d4d2d6a50851c4197c8bec0fc5e0c634682da8f5a3d4815165c`.
- `top100-candidates.json` (lexical-v1) SHA-256 : `b2b93bccdfb0fa64540eaacc3178b8e7a51125c71b426b5e9582d43e7b6b4738`.
- `semantic-top100-candidates.json` (semantic-cortex-v1) SHA-256 : `20893569440e38d4b8febb56adf0e92cbf0b95d79531394048b8bb25e19fa8f5`.

Toutes les 162 paires de l'union possèdent une adjudication dans le fichier de labels. `model_id` et `prompt_sha256` sont explicitement nuls : T0 n'exécute aucun juge et n'utilise aucun prompt.

## Recommandation : parquer / changer d'approche (décomposition en claims)

Parquer le fork LLM-juge sur ce pool. À cutoff 100, il ne pourrait juger que 3 des 20 contradictions synthétiques ; même avec une classification parfaite, son rappel resterait à 0,15. À épuisement des 162 paires uniques, le plafond ne monte qu'à 0,20.

Le prochain fork devrait agir sur la génération, par exemple en décomposant chaque note en claims atomiques (sujet, portée, polarité, valeur/état et contexte temporel), puis en appariant les claims compatibles avant jugement. Il faut remesurer le plafond de rappel de ce nouveau pool avant tout investissement dans un juge génératif.

STOP : aucun provider LLM-juge, prompt, cache ni détecteur de production n'est implémenté dans ce lot.
