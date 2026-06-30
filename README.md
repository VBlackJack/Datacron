# Datacron

> Serveur MCP local pour interroger et maintenir un vault Markdown depuis Claude Desktop
> ou Claude Code, sans envoyer tout le vault dans le contexte.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![MCP: local stdio](https://img.shields.io/badge/MCP-local_stdio-purple)](#mcp-tools)

Datacron indexe un dossier de notes Markdown, expose un serveur MCP local, puis renvoie
au client les notes ou chunks pertinents au lieu d'un dump complet. Le vault reste un
dossier Markdown normal : Datacron ajoute seulement un sidecar `.datacron/` pour l'index,
les logs, les ULID internes et les backups.

## Ce qui est en place

| Surface | État actuel |
|---|---|
| Lecture vault | `list_notes`, `get_note`, resources `datacron://vault/map`, `vault/info`, `policy/active` |
| Recherche | SQLite FTS5/BM25, query-expansion FR↔EN, re-rank temporel, `ripgrep` via `search_regex` |
| Graphe local | Wikilinks et backlinks via `get_backlinks` |
| Écriture | `create_note_ai` et `append_journal`, désactivés par défaut sans `DATACRON_WRITE_PATHS` |
| Index | `datacron index` incrémental, `datacron reindex` complet, réparation automatique à la lecture |
| Évaluation | `datacron eval` avec recall@k, precision, latence et tokens |
| Clients testés | Claude Desktop via installateur, Claude Code via serveur stdio |

Mesure actuelle sur le golden set Julien, avec query-expansion et temporal re-rank actifs :

```text
recall@5  0.89
recall@10 0.95
recall@20 0.95
precision 0.32
tokens    39984
```

## Installation

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
- Claude Desktop ou un autre client MCP stdio

## Démarrage rapide

```bash
datacron init /path/to/vault
datacron index --vault /path/to/vault
datacron status --vault /path/to/vault
datacron mcp install --client claude-desktop --vault /path/to/vault
```

Redémarre Claude Desktop après `datacron mcp install`.

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
| `DATACRON_READ_PATHS` | vide | allowlist de lecture ; l'installateur Claude Desktop la fixe au vault |
| `DATACRON_WRITE_PATHS` | vide | allowlist d'écriture ; vide = write tools désactivés |
| `DATACRON_MAX_RESULT_COUNT` | `20` | nombre max de résultats retournés |
| `DATACRON_MAX_RESULT_TOKENS` | `8000` | budget token des résultats de recherche |
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

Garanties :

- confinement strict dans `DATACRON_WRITE_PATHS`
- overwrite atomique via fichier temporaire + `os.replace`
- backup horodaté sous `.datacron/backups/` avant modification d'une note existante
- `reconcile()` après write pour rendre la note immédiatement cherchable
- audit log local

Le mode concurrent multi-machines n'est pas supporté pour les écritures : garde une règle
single-writer sur le vault.

## MCP Tools

| Tool | Description |
|---|---|
| `list_notes` | liste les notes, filtrable par dossier et tags |
| `get_note` | lit une note par ULID, chemin relatif ou chunk id ; formats `full` et `map` |
| `search_text` | recherche BM25 avec query-expansion et re-rank temporel |
| `search_regex` | recherche regex via ripgrep, avec résolution vers chunks indexés |
| `get_backlinks` | trouve les chunks qui pointent vers une note ou un alias |
| `create_note_ai` | crée une note Markdown typée, si writes activés |
| `append_journal` | ajoute une entrée sous un heading, si writes activés |

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
- Le client MCP, par exemple Claude Desktop, peut envoyer à son fournisseur les chunks que
  Datacron lui retourne. Datacron ne lui envoie pas le vault complet.
- Le contenu retourné aux clients est enveloppé dans `<vault_content>...</vault_content>`.
- Les résultats sont bornés par nombre et par budget token.
- Les accès filesystem sont confinés par `DATACRON_READ_PATHS` et `DATACRON_WRITE_PATHS`.
- Les opérations MCP sont auditées dans les logs locaux.

## Commandes CLI

```bash
datacron init /path/to/vault
datacron status --vault /path/to/vault
datacron index --vault /path/to/vault
datacron reindex --vault /path/to/vault
datacron eval --questions local/golden-julien.yaml --vault /path/to/vault
datacron mcp serve --vault /path/to/vault
datacron mcp install --client claude-desktop --vault /path/to/vault
```

## Limites actuelles

- Pas de vector search / embeddings : la mesure actuelle ne le justifie pas.
- Pas d'agent autonome : le client MCP orchestre.
- Pas de GUI.
- Pas de writes concurrents multi-machines.
- L'installateur automatique ne cible aujourd'hui que Claude Desktop. Les autres clients MCP
  peuvent utiliser `datacron mcp serve` ou `datacron-mcp` en stdio si leur configuration le permet.

## Développement

```bash
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy
pytest
```

Dernier état vérifié sur cette branche :

```text
387 passed
ruff OK
ruff format --check OK
mypy --strict OK
```

## Licence

Copyright 2026 Julien Bombled.

Licensed under the [Apache License, Version 2.0](LICENSE).
