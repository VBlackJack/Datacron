# Datacron

> Serveur MCP local pour interroger et maintenir un vault Markdown depuis Claude, Codex,
> Gemini ou un autre client MCP stdio, sans envoyer tout le vault dans le contexte.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![MCP: local stdio](https://img.shields.io/badge/MCP-local_stdio-purple)](#mcp-tools)
[![CI](https://github.com/VBlackJack/datacron/actions/workflows/ci.yml/badge.svg)](https://github.com/VBlackJack/datacron/actions/workflows/ci.yml)

**Français** | [English](README.en.md)

Datacron indexe un dossier de notes Markdown, expose un serveur MCP local, puis renvoie
au client les notes ou chunks pertinents au lieu d'un dump complet. Le vault reste un
dossier Markdown normal : Datacron ajoute seulement un sidecar `.datacron/` pour l'index,
les logs, les ULID internes, l'historique et le journal d'opérations.

## Ce qui est en place

| Surface | État actuel |
|---|---|
| Lecture vault | `list_notes`, `get_note`, resources `datacron://vault/map`, `vault/info`, `policy/active` |
| Recherche | SQLite FTS5/BM25, query-expansion FR↔EN, re-rank temporel, `ripgrep` via `search_regex` |
| Graphe local | Wikilinks et backlinks via `get_backlinks` |
| Écriture | 5 tools confinés et réversibles, désactivés par défaut sans `DATACRON_WRITE_PATHS` |
| Index | `datacron index` incrémental, `datacron reindex` complet, réparation automatique à la lecture |
| Évaluation | `datacron eval` sur le pipeline MCP réel : recall@k, MRR, nDCG, fraîcheur, latence et payload tokens |
| Setup guidé | `datacron setup` : init + index + enregistrement MCP en une commande |
| Clients | Auto-détection et enregistrement via `datacron setup --client all` : Claude Desktop, Claude Code, Cursor, Gemini CLI, Codex CLI, Windsurf, VS Code |
| Distribution | Installeur Windows (`Datacron-Setup.exe`), exécutable autonome (PyInstaller) sans Python requis, ou installation depuis les sources |

Mesure locale du pipeline `tool/impl` réellement reçu par l'agent, 19 questions,
configuration 8k tokens / 20 résultats, 17 juillet 2026 :

```text
recall@5       0.89
recall@10      0.95
recall@20      0.95
MRR            0.73
nDCG@10        0.79
latence p50    57 ms
latence p95    276 ms
payload tokens 90567
```

Le tool égale désormais le store brut à recall@5 (0,89) : le delta précédent venait de la
comparaison globale de scores issus des requêtes AND et OR, pas du budget ni d'une limite de
BM25. Le throttle repair-on-read ramène sa p50 propre à 0,009 ms ; le premier sweep complet
de la session reste visible dans la p95. Le golden ne contient pas encore de cas
`forbidden_paths` et le vault n'a pas de relation `supersedes` indexée.

## Installation

### Windows : installeur en un double-clic

Le plus simple sous Windows : télécharge `Datacron-Setup.exe` depuis la
[dernière Release](https://github.com/VBlackJack/datacron/releases/latest), double-clique,
et choisis ton vault. Aucun Python, aucun terminal, aucun droit administrateur ; Datacron
s'enregistre automatiquement dans tes clients IA. Guide détaillé :
[Installation sous Windows](docs/fr/installation-windows.md).

### Depuis les sources

Depuis un clone du repo :

```bash
python -m pip install -e ".[dev]"
```

Ou, pour installer seulement l'application :

```bash
python -m pip install -e .
```

Prérequis runtime :

- Python 3.11+
- `ripgrep` disponible dans le `PATH` pour `search_regex`
- un dossier de notes Markdown
- un client MCP stdio pris en charge, par exemple Claude Desktop, Codex CLI ou Gemini CLI

## Démarrage rapide

Le plus simple - une commande détecte tes clients IA, initialise le vault, l'indexe et
enregistre Datacron partout :

```bash
datacron setup            # interactif ; ajoute --yes pour tout par défaut
```

Voir le [guide d'installation](docs/fr/setup.md) pour les options (`--client`, `--scope`,
écriture, durabilité). Ou étape par étape :

```bash
datacron init /path/to/vault
datacron index --vault /path/to/vault
datacron status --vault /path/to/vault
datacron mcp install --client claude-desktop --vault /path/to/vault
```

La sous-commande `mcp install` ci-dessus est dédiée à Claude Desktop. Pour Codex CLI,
Gemini CLI, Cursor et les autres clients, utilise le setup multi-client avec
`datacron setup --client <identifiant>` ou l'auto-détection avec `--client all`.

Redémarre le ou les clients configurés après l'installation.

Pour lancer le serveur manuellement :

```bash
datacron mcp serve --vault /path/to/vault
```

L'entrée script directe utilisée par l'installateur est aussi disponible :

```bash
datacron-mcp
```

`datacron-mcp` lit le vault depuis `DATACRON_VAULT_ROOT`.

## Configuration

`datacron init` crée `.datacron/VAULT.yaml`. Ce fichier peut porter la configuration
vault-local, notamment la query-expansion :

```yaml
query_expansion:
  supervision: [monitoring]
  sauvegarde: [backup]
  restauration: [restore]
  chiffrement: [encryption]
  sécurité: [security]
  validité: [validity]
  certificat: [certificate]
```

Variables d'environnement utiles :

| Variable | Défaut | Rôle |
|---|---:|---|
| `DATACRON_VAULT_ROOT` | répertoire courant ou `--vault` | vault servi par le serveur |
| `DATACRON_READ_PATHS` | vide | allowlist de lecture ; le setup des clients la fixe au vault |
| `DATACRON_WRITE_PATHS` | vide | allowlist d'écriture ; vide = write tools désactivés |
| `DATACRON_MAX_RESULT_COUNT` | `20` | nombre max de résultats retournés |
| `DATACRON_MAX_RESULT_TOKENS` | `8000` | budget token des résultats de recherche |
| `DATACRON_REPAIR_MIN_INTERVAL_SECONDS` | `30` | intervalle minimal entre les sweeps repair-on-read ; `0` = chaque lecture |
| `DATACRON_GET_NOTE_MAX_TOKENS` | `25000` | budget de `get_note(format="full")` |
| `DATACRON_CHUNK_MAX_TOKENS` | `1024` | taille cible max des chunks |
| `DATACRON_RIPGREP_PATH` | `rg` | binaire ripgrep |

Les listes de chemins utilisent le séparateur de l'OS (`:` sous Unix, `;` sous Windows).

## Écriture

Les writes sont volontairement OFF par défaut. Sans `DATACRON_WRITE_PATHS`, les tools
d'écriture renvoient une erreur claire et ne créent aucun fichier.

Pour activer l'écriture sur un sous-dossier précis :

```powershell
$env:DATACRON_VAULT_ROOT = "G:\_DATA"
$env:DATACRON_READ_PATHS = "G:\_DATA"
$env:DATACRON_WRITE_PATHS = "G:\_DATA\_memory"
datacron mcp serve --vault G:\_DATA
```

Tools d'écriture disponibles :

- `create_note_ai` : crée une note Markdown typée, sans overwrite.
- `append_journal` : ajoute une entrée sous un heading d'une note existante.
- `set_frontmatter` : met à jour les champs de cycle de vie sans modifier le corps Markdown.
- `patch_note_section` : remplace le contenu sous un heading existant avec contrôle CAS.
- `revert_note` : restaure les octets exacts d'une version conservée dans l'historique.

Garanties :

- confinement strict dans `DATACRON_WRITE_PATHS`
- overwrite atomique via fichier temporaire + `os.replace`
- historique adressé par contenu avant modification d'une note existante
- `reconcile()` après write pour rendre la note immédiatement cherchable
- audit log local

Le mode concurrent multi-machines n'est pas supporté pour les écritures : garde une règle
single-writer sur le vault.

## MCP Tools

### Lecture

| Tool | Description |
|---|---|
| `list_notes` | retourne une liste paginée, filtrable par dossier et tags, avec ULID, titre, tags, alias et dates |
| `get_note` | lit une note par ULID, chunk id ou chemin relatif, en contenu paginé, chunk ou plan de headings |
| `search_text` | effectue une recherche BM25 sur l'index FTS5 avec snippets classés et notes obsolètes démotées par défaut |
| `search_regex` | effectue une recherche regex via ripgrep et résout les lignes trouvées vers les chunks indexés |
| `get_backlinks` | retourne les chunks dont les wikilinks ciblent un ULID ou un alias résolu |

### Écriture

| Tool | Description |
|---|---|
| `create_note_ai` | crée une nouvelle note `_memory` typée, confinée aux chemins autorisés, sans overwrite et avec journal durable |
| `append_journal` | ajoute une entrée Markdown sous un heading, avec confinement, historique exact et écriture atomique |
| `set_frontmatter` | modifie uniquement les champs de cycle de vie et la date `updated`, en préservant le corps Markdown |
| `patch_note_section` | remplace le contenu d'un heading existant avec CAS, historique exact et préservation des autres sections |
| `revert_note` | restaure une note depuis son historique adressé par contenu ; l'opération reste durable, réversible et auditée |

### Opérationnel

| Tool | Description |
|---|---|
| `get_health` | retourne l'état réel de fraîcheur de l'index, d'intégrité, de checksum, de durabilité et des invariants |
| `get_note_history` | liste les métadonnées d'opérations validées d'une note sans lire le contenu historique ni modifier le journal |
| `audit_query` | interroge les métadonnées d'opérations par période, tool ou note sans modifier le journal ni le vault |

### Advisory (expérimental)

| Tool | Description |
|---|---|
| `contradiction_scan` | scan live, déterministe et borné des contradictions/raffinements entre sections ; propose puis confirme en lecture seule un appel CAS explicite, sans jamais écrire automatiquement |

Resources MCP :

- `datacron://vault/map`
- `datacron://vault/info`
- `datacron://policy/active`

## Recherche

`search_text` combine plusieurs signaux :

- FTS5/BM25 pour le score lexical de base
- query-expansion FR↔EN configurée dans `VAULT.yaml`
- re-rank temporel conservateur :
  - une note citée dans le `supersedes` d'une autre est fortement démotée
  - `confidence: low` et `confidence: needs_verification` appliquent une pénalité légère
  - `include_superseded=true` permet de remonter les notes historiques

`search_regex` reste littéral : il n'applique ni query-expansion ni re-rank temporel.

## Vie privée et sécurité

- Datacron ne fait pas de télémétrie.
- Datacron n'appelle pas de LLM cloud.
- Le client MCP, par exemple Claude, Codex ou Gemini, peut envoyer à son fournisseur les
  chunks que Datacron lui retourne. Datacron ne lui envoie pas le vault complet.
- Le contenu retourné aux clients est enveloppé dans `<vault_content>...</vault_content>`.
- Les résultats sont bornés par nombre et par budget token.
- Les accès filesystem sont confinés par `DATACRON_READ_PATHS` et `DATACRON_WRITE_PATHS`.
- Les opérations MCP sont auditées dans les logs locaux.

## Commandes CLI

```bash
datacron setup                      # parcours guidé : init + index + config client
datacron setup --yes                # tout par défaut, sans question
datacron setup --client all --scope both --vault /path/to/vault
datacron init /path/to/vault
datacron status --vault /path/to/vault
datacron index --vault /path/to/vault
datacron reindex --vault /path/to/vault
datacron scrub-init --vault /path/to/vault
datacron scrub --vault /path/to/vault
datacron eval --questions examples/eval-questions.example.yaml --vault /path/to/vault
datacron eval --questions local/golden.yaml --vault /path/to/vault --save-baseline
datacron eval --questions local/golden.yaml --vault /path/to/vault --compare --json
datacron mcp serve --vault /path/to/vault
datacron mcp install --client claude-desktop --vault /path/to/vault  # dédié Claude Desktop
datacron unregister --client all --scope both --vault /path/to/vault
```

## Limites actuelles

- Pas de vector search / embeddings : le spike est écarté sur le golden actuel, car le
  recall@5 tool-level à 0,89 égale le store BM25. À réévaluer si un golden élargi retombe
  sous 0,85 avec la même évaluation.
- Pas d'agent autonome : le client MCP orchestre.
- Pas de GUI.
- Pas de writes concurrents multi-machines.
- La détection des clients par `datacron setup` est best-effort (présence d'un dossier de config
  ou d'un binaire sur le `PATH`) ; une installation dans un emplacement non standard peut être
  manquée et se configure alors à la main.

## Documentation

Sommaire complet : [docs/fr/index.md](docs/fr/index.md) | [English index](docs/en/index.md).

Pour démarrer :

- [Guide d'installation et de configuration](docs/fr/setup.md)
- [Guide utilisateur](docs/fr/user-guide.md)

Références techniques :

- [Conventions du vault (SPEC)](docs/fr/spec.md)
- [Architecture et surface publique](docs/fr/architecture.md)
- [Décisions tranchées v2.1](docs/fr/decisions-v2.1.md)
- [Frontière de sécurité](docs/fr/security-boundary.md)
- [Scrubber d'intégrité](docs/fr/integrity-scrubber.md)
- [Santé opérationnelle et durabilité](docs/fr/operational-health.md)
- [Contrat de fraîcheur](docs/fr/freshness-contract-v1.md)

## Développement

```bash
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy
pytest
```

## Licence

Copyright 2026 Julien Bombled.

Licensed under the [Apache License, Version 2.0](LICENSE).
