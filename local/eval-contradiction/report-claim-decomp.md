# Mesure du générateur par décomposition en claims

Date : 2026-07-13
Statut : PASS — LOT 10 éligible

## Verdict

Le pool plein récupère 11/20 positifs synthétiques (rappel 0.55) contre le plafond LOT 8 de 0.20.
Le générateur émet 357 paires uniques, soit 13.97% des 2556 paires possibles, pour un budget maximal de 400.

Garde rappel : PASS ; garde budget : PASS.

## Rappel et taille du ranking

| Cutoff | Candidats examinés | Positifs synthétiques | Rappel | Positifs curation |
|---:|---:|---:|---:|---:|
| 25 | 25 | 1/20 | 0.05 | 0/4 |
| 50 | 50 | 1/20 | 0.05 | 0/4 |
| 100 | 100 | 3/20 | 0.15 | 0/4 |
| 200 | 200 | 10/20 | 0.50 | 0/4 |
| pool plein | 357 | 11/20 | 0.55 | 0/4 |

## Rappel synthétique par strate, pool plein

| Strate | Présents | Rappel |
|---|---:|---:|
| activation_deactivation | 3/5 | 0.60 |
| obligation_interdiction | 1/5 | 0.20 |
| replacement_state | 4/5 | 0.80 |
| value_threshold | 3/5 | 0.60 |

## Canary naturelle

Canary absente du pool.
Elle reste séparée de toutes les métriques synthétiques.

## Méthode

Le provider découpe les titres et corps du corpus figé en claims atomiques. Les alias, marqueurs, regex de valeur et seuils vivent dans les fichiers de règles/configuration. Les sujets compatibles sont rapprochés avec des termes rares pondérés par IDF ; une paire n'est émise que sur conflit potentiel de polarité ou de valeur/état. Aucun LLM, réseau ou accès au vault n'est utilisé.

Le budget est mesuré sur la sortie pleine non tronquée. Le ranking complet est inclus dans l'évidence afin de permettre un recalcul indépendant.

## Déterminisme et intégrité

Déterminisme : PASS.
- Run 1 : ranking SHA-256 `8b740b16a77d5b13b233c9d8c9ca04c2c55a5b1294710c6fc8b80f7649b83eec`, 20.613 s.
- Run 2 : ranking SHA-256 `8b740b16a77d5b13b233c9d8c9ca04c2c55a5b1294710c6fc8b80f7649b83eec`, 20.685 s.
- Run 3 : ranking SHA-256 `8b740b16a77d5b13b233c9d8c9ca04c2c55a5b1294710c6fc8b80f7649b83eec`, 20.684 s.

- `corpus.json` SHA-256 : `c1dbfee8d4f1df41c34a9e06cd5d1eab9d3619816e2106e8d29e71de90fd1b42`.
- `labels.jsonl` SHA-256 : `28c0a51aa2b72d4d2d6a50851c4197c8bec0fc5e0c634682da8f5a3d4815165c`.
- `synthetic-spec.json` SHA-256 : `54f600d6f55145e0981dacb59dd0dcecaad0367b9fed27e19c3005d546c2dd44`.
- Règles SHA-256 : `6cd7b34e3c1a272ec53786aaa72ea1902338f7fc301856dc9fd6d213f2cf5368`.

Le fichier de labels courant contient 274 paires ; il est réutilisé tel quel, sans réétiquetage. `model_id` et `prompt_sha256` sont nuls car l'extraction est entièrement fondée sur des règles locales.

## Fork à trancher par Julien

Les deux gardes sont franchies : un LOT 10 peut mesurer un juge sur ce pool, sans que ce handoff constitue une décision de production. Une contre-vérification superviseur doit recalculer rappel et budget et muter les règles pour vérifier la réaction des gardes avant merge.

Aucun détecteur de production, juge LLM, câblage MCP ou changement `src/` n'est inclus dans ce lot.
