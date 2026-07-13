# Mesure du générateur par décomposition en claims

Date : 2026-07-13
Statut : PASS — LOT 10 éligible

## Verdict

Le pool plein récupère 15/20 positifs synthétiques (rappel 0.75) contre le plafond LOT 9 de 0.55.
Le générateur émet 393 paires uniques, soit 15.38% des 2556 paires possibles, pour un budget maximal de 400.

Gardes : rappel PASS, budget PASS, anti-all-pairs PASS, strate cible PASS, non-régression PASS, déterminisme PASS et anti-fuite PASS.

## Rappel et taille du ranking

| Cutoff | Candidats examinés | Positifs synthétiques | Rappel | Positifs curation |
|---:|---:|---:|---:|---:|
| 25 | 25 | 3/20 | 0.15 | 0/4 |
| 50 | 50 | 5/20 | 0.25 | 0/4 |
| 100 | 100 | 7/20 | 0.35 | 0/4 |
| 200 | 200 | 8/20 | 0.40 | 0/4 |
| pool plein | 393 | 15/20 | 0.75 | 1/4 |

## Rappel synthétique par strate, pool plein

| Strate | Présents | Rappel |
|---|---:|---:|
| activation_deactivation | 5/5 | 1.00 |
| obligation_interdiction | 3/5 | 0.60 |
| replacement_state | 4/5 | 0.80 |
| value_threshold | 3/5 | 0.60 |

## Curation et delta vs LOT 9

Curation : 1/4 (0.25), mesure **in-sample et diagnostique uniquement**. Elle n'entre dans aucune gate ; sa validation est différée à un pilote externe.

Le pool gagne +36 paires vs LOT 9 (357 → 393) et +0.20 de rappel synthétique.

Positifs nouvellement récupérés :
- `_memory/facts/concurrent-agents-corrupt-git-shared-drive.md` ↔ `synthetic/syn-obligation-01.md` (synthetique, obligation_interdiction, rang 39).
- `_memory/projects/datacron-karpathy-wiki-design.md` ↔ `synthetic/syn-obligation-05.md` (synthetique, obligation_interdiction, rang 368).
- `_memory/projects/heimdall-updater.md` ↔ `synthetic/syn-activation-01.md` (synthetique, activation_deactivation, rang 182).
- `_memory/projects/heimdall-per-profile-logging.md` ↔ `synthetic/syn-activation-02.md` (synthetique, activation_deactivation, rang 25).
- `_memory/facts/skills-architecture.md` ↔ `_memory/preferences/julien.md` (curation, sans strate, rang 282).
- Aucun positif LOT 9 perdu.

## Canary naturelle

Canary présente du pool au rang 282.
Elle reste séparée de toutes les métriques synthétiques.

## Règles ajoutées et anti-fuite

- `longest-alias-first` — Les modaux composés comme 'ne doit pas' doivent être normalisés avant leurs sous-termes positifs.
- `deontic-tristate` — Obligation, interdiction et permission sont des polarités distinctes applicables à toute règle ou politique.
- `exclusive-vs-concurrent` — L'exclusivité et l'exécution concurrente sont des états incompatibles d'une même politique d'accès.
- `revision-lifecycle` — Faux/corrigé/résolu/re-déféré/livré décrivent des révisions d'état générales, sans dépendre d'une note.
- `storage-form-and-authority` — Fichier plat, magasin, Markdown canonique et store dérivé sont des choix d'état incompatibles récurrents.
- `cardinality-and-keyed-values` — Les cardinalités et valeurs numériques divergentes sur le même champ sont des conflits génériques.
- `frontmatter-supersedes-context` — Une relation supersedes autorise un appariement plus sensible, mais seulement si des claims sémantiques divergent.

Scan anti-fuite : PASS, 38 chemins et 38 identifiants positifs recherchés dans 4 fichiers de code/règles/config.
Aucune règle n'est indexée sur l'identité d'une note ou paire labellisée.

## Méthode

Le provider découpe les titres et corps du corpus figé en claims atomiques. Les alias, marqueurs, regex de valeur et seuils vivent dans les fichiers de règles/configuration. Les sujets compatibles sont rapprochés avec des termes rares pondérés par IDF ; une paire n'est émise que sur conflit potentiel de polarité ou de valeur/état. Aucun LLM, réseau ou accès au vault n'est utilisé.

Le budget est mesuré sur la sortie pleine non tronquée. Le ranking complet est inclus dans l'évidence afin de permettre un recalcul indépendant.

## Déterminisme et intégrité

Déterminisme : PASS.
- Run 1 : ranking SHA-256 `8e49dcb03810d57d07f785261faa730f150570b34898268824ee2cff5ba634f2`, 31.256 s.
- Run 2 : ranking SHA-256 `8e49dcb03810d57d07f785261faa730f150570b34898268824ee2cff5ba634f2`, 30.335 s.
- Run 3 : ranking SHA-256 `8e49dcb03810d57d07f785261faa730f150570b34898268824ee2cff5ba634f2`, 32.270 s.

- `corpus.json` SHA-256 : `c1dbfee8d4f1df41c34a9e06cd5d1eab9d3619816e2106e8d29e71de90fd1b42`.
- `labels.jsonl` SHA-256 : `28c0a51aa2b72d4d2d6a50851c4197c8bec0fc5e0c634682da8f5a3d4815165c`.
- `synthetic-spec.json` SHA-256 : `54f600d6f55145e0981dacb59dd0dcecaad0367b9fed27e19c3005d546c2dd44`.
- Règles SHA-256 : `34becbf80ebd8e5a312e27816cd6e4ada8cd805cd8393bac8660cfc800155268`.
- Évidence LOT 9 SHA-256 : `5013b9696100a8f61bd14da1886d2d7ba81e4ef5fade90d51821a71ff3d3a962`.

Le fichier de labels courant contient 274 paires ; il est réutilisé tel quel, sans réétiquetage. `model_id` et `prompt_sha256` sont nuls car l'extraction est entièrement fondée sur des règles locales.

## Fork à trancher par Julien

Toutes les gardes sont franchies : un LOT 10 peut mesurer un juge sur ce pool, sans que ce handoff constitue une décision de production. Une contre-vérification superviseur doit recalculer rappel, strates et pool depuis les artefacts, puis inspecter les règles contre la fuite avant merge.

Aucun détecteur de production, juge LLM, câblage MCP ou changement `src/` n'est inclus dans ce lot.
