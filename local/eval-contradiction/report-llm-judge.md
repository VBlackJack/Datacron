# Mesure du LLM-juge sur ranking figé

Date : 2026-07-13
Statut : MESURE VALIDE — aucune décision de production

## Verdict

Sur les 139 paires adjugées, le juge conserve 13 vraies contradictions et 6 négatives connues. Sa précision est 0.684, contre 0.115 pour le baseline keep-all.
Rappel-juge : 13/16 = 0.812. Rappel bout-en-bout plafonné : 0.75 x 0.812 = 0.609.
Signal de viabilité v1 (non décisionnel) : PASS ; Julien conserve seul la décision go-production.

## Frontière de vérité terrain

| Population | Total | Déclarées contradiction | Usage |
|---|---:|---:|---|
| Adjugées | 139 (16 positives, 123 négatives) | 19 | Précision et rappel réels |
| Non jugées | 254 | 5 | Backlog d'adjudication uniquement |

Aucune précision n'est inférée sur les 254 paires non jugées.

## Rappel des positives adjugées par strate

| Strate | Conservées | Rappel-juge |
|---|---:|---:|
| activation_deactivation | 5/5 | 1.000 |
| curation | 0/1 | 0.000 |
| obligation_interdiction | 3/3 | 1.000 |
| replacement_state | 2/4 | 0.500 |
| value_threshold | 3/3 | 1.000 |

## Erreurs sur les paires adjugées

Faux positifs restants par portée prédite :
- `activation` : 1.
- `obligation` : 2.
- `replacement_state` : 2.
- `value_threshold` : 1.

Faux négatifs :
- `_memory/facts/skills-architecture.md` ↔ `_memory/preferences/julien.md` (curation, confiance 0.990) : Les notes concernent des sujets et portées distinctes (architecture vs préférences workflow).
- `_memory/projects/heimdall-testenv.md` ↔ `synthetic/syn-state-04.md` (replacement_state, confiance 0.950) : Les notes décrivent des environnements et protocoles différents, pas des affirmations incompatibles.
- `_memory/projects/themeforge-magellan.md` ↔ `synthetic/syn-state-05.md` (replacement_state, confiance 0.950) : Les deux notes décrivent des sujets et portées différents.

## Entrée figée, cache et coût

- Évidence v2 SHA-256 : `eb421b383c460440cc4749ef649f3f7e23280b8d91d282d079b411f19c80012a`.
- Ranking canonique SHA-256 : `f6e1c1d31a40da7134b285dce1f0690bd033680b22d1fcac8cbc2aa9c619681b`.
- Modèle demandé : `qwen3:8b`.
- Version(s) modèle retournée(s) : `qwen3:8b`.
- Prompt SHA-256 : `60a53b334df6c605dd9baa37c8669c7703d3c8364efe158ced2d84a61536dd93`.
- Température : 0.0; seuil : 0.70.
- Cache : 393 entrées, SHA-256 `4f67b276b55d01e36c82479032d938d73324f01c6fad7fbd5990243e79d5e0e5`.
- Génération initiale : 1152518 tokens entrée, 26528 sortie, latence cumulée 648.494 s, coût plafond estimé 0.0000 USD.

## Déterminisme replayable

- Replay 1 : `f1db36e3e428677e0daf185360d2e729daa4e12d31624e2fb5cc089f74f5cb7c`, 24 paires, 6.668 s, cache misses = 0.
- Replay 2 : `f1db36e3e428677e0daf185360d2e729daa4e12d31624e2fb5cc089f74f5cb7c`, 24 paires, 6.642 s, cache misses = 0.
- Replay 3 : `f1db36e3e428677e0daf185360d2e729daa4e12d31624e2fb5cc089f74f5cb7c`, 24 paires, 6.753 s, cache misses = 0.

## Gardes

- Entrée figée : PASS.
- Frontière 139/254 : PASS.
- Déterminisme cache-only : PASS.
- Anti-fuite : PASS (3 few-shot, 68 chemins pool scannés).
- Non-destruction rappel : PASS (13/16, minimum 13).

Le provider de mesure n'effectue aucun accès réseau pendant les trois replays : toute absence du cache provoque un STOP. Aucun changement `src/`, câblage MCP, gate wiki ou détecteur de production n'est inclus.

Ce handoff doit être contre-vérifié depuis le cache, les labels et le ranking par le superviseur avant merge. Il ne constitue pas un go-production.
