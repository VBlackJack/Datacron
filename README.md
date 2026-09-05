# Datacron

> Serveur MCP local pour interroger et maintenir un vault Markdown depuis Claude, Codex,
> Gemini ou un autre client MCP stdio, sans envoyer tout le vault dans le contexte.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![MCP: local stdio](https://img.shields.io/badge/MCP-local_stdio-purple)](#mcp-tools)
[![CI](https://github.com/VBlackJack/datacron/actions/workflows/ci.yml/badge.svg)](https://github.com/VBlackJack/datacron/actions/workflows/ci.yml)

**Français** | [English](README.en.md)

## À quoi sert Datacron ?

Retrouver le contexte d'un projet, préparer un échange et garder la trace des engagements :
Datacron donne à ton assistant accès à une mémoire durable, lisible et modifiable en Markdown.
Les notes restent utilisables indépendamment du client choisi.

| Besoin | Exemple de demande à ton assistant |
|---|---|
| Reprendre un projet | « Où en étions-nous ? Retrouve les décisions et les prochaines actions. » |
| Préparer une réunion | « Résume nos derniers échanges et les points encore ouverts, avec leurs sources. » |
| Retrouver une personne | « Qui est cette personne, dans quel contexte l'ai-je rencontrée et que devons-nous suivre ? » |
| Suivre des objectifs | « Retrouve les engagements et les réalisations utiles à mon prochain entretien. » |
| Garder une trace fiable | « Enregistre cette décision, rattache-la au projet et vérifie qu'elle est sauvegardée. » |

L'assistant orchestre ces demandes avec les outils disponibles et les droits accordés.
Le protocole commun guide la lecture, l'enrichissement des fiches personnes et la vérification
des écritures. Une identité ambiguë demande clarification ; une échéance enregistrée ne programme
pas de rappel. [Découvrir le suivi quotidien](docs/fr/memory-discipline.md).

**Commencer :** [installer](#installation) · [première session](#première-session) ·
[guide utilisateur](docs/fr/user-guide.md) · [référence MCP](#mcp-tools) ·
[vie privée](#vie-privée-et-sécurité).

## Installation

### Windows : installeur en un double-clic

Le plus simple sous Windows : télécharge `Datacron-Setup.exe` depuis la
[dernière Release](https://github.com/VBlackJack/datacron/releases/latest), double-clique,
et choisis ton vault. Aucun Python, aucun terminal, aucun droit administrateur ; Datacron
s'enregistre automatiquement dans tes clients IA. Guide détaillé :
[Installation sous Windows](docs/fr/installation-windows.md).

### Python : depuis PyPI

```bash
python -m pip install datacron
datacron setup
```

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

## Première session

1. Choisis ton dossier de notes avec l'installeur ou `datacron setup`.
2. Reconnecte Datacron dans ton client MCP pour charger les outils et les instructions.
3. Demande : « Retrouve les notes de mon projet et résume son état avec les sources. »

Pour les sessions de mémoire, `session_context` fournit un contexte borné et le protocole
commun. `prepare_follow_up` prépare les mises à jour sourcées ; les outils d'écriture les
appliquent selon les permissions. `get_follow_up` retrouve les dernières révisions structurées.
Les anciennes notes en prose restent à consulter ; elles ne sont pas converties automatiquement.

Le serveur travaille localement. Ton client peut transmettre les extraits retournés à son
fournisseur de modèle : voir [vie privée et sécurité](#vie-privée-et-sécurité).

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
Gemini CLI, Antigravity, LM Studio, Cursor et les autres clients, utilise le setup multi-client avec
`datacron setup --client <identifiant>` ou l'auto-détection avec `--client all`.

### Ajouter Datacron à LM Studio

LM Studio 0.3.17+ possède une configuration utilisateur unique et aucun scope projet. La
commande recommandée est :

```bash
datacron setup --yes --vault "CHEMIN_VAULT" --client lmstudio --scope user
```

Pour une installation Python où `datacron-mcp` est dans le `PATH`, la configuration
équivalente en lecture seule peut aussi être importée avec ce deeplink officiel :

[Add to LM Studio](lmstudio://add_mcp?name=datacron&config=eyJjb21tYW5kIjoiZGF0YWNyb24tbWNwIiwiYXJncyI6W10sImVudiI6eyJEQVRBQ1JPTl9WQVVMVF9ST09UIjoiPFlPVVJfVkFVTFQ%2BIiwiREFUQUNST05fUkVBRF9QQVRIUyI6IjxZT1VSX1ZBVUxUPiIsIkRBVEFDUk9OX0RVUkFCSUxJVFkiOiJiZXN0LWVmZm9ydCJ9fQ%3D%3D)

Le lien importe cet exemple. Ouvre l'éditeur MCP de LM Studio et remplace les deux
placeholders `<YOUR_VAULT>` avant de démarrer le serveur :

```json
{
  "mcpServers": {
    "datacron": {
      "command": "datacron-mcp",
      "args": [],
      "env": {
        "DATACRON_VAULT_ROOT": "<YOUR_VAULT>",
        "DATACRON_READ_PATHS": "<YOUR_VAULT>",
        "DATACRON_DURABILITY": "best-effort"
      }
    }
  }
}
```

L'exemple n'active pas les outils d'écriture. Le setup CLI est plus sûr pour les
installations packagées, car il écrit automatiquement le vrai chemin de l'exécutable.

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
| `DATACRON_VAULT_ROOT` | non définie | fallback après `--vault` ; le répertoire courant n'est accepté que s'il contient `.datacron/VAULT.yaml` |
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

`datacron setup` peut aussi poser l'allowlist au niveau du poste (variable
d'environnement utilisateur, opt-in) pour que tous les clients MCP en héritent ;
défaut : `_memory`, `_drafts`, `_journal`. Voir le [guide d'installation](docs/fr/setup.md).

Tools d'écriture disponibles :

- `create_note_ai` : crée une note Markdown typée, sans overwrite.
- `append_journal` : ajoute une entrée sous un heading d'une note existante.
- `set_frontmatter` : met à jour les champs de cycle de vie et la liste `rejected` (options écartées) sans modifier le corps Markdown.
- `patch_note_preamble` : remplace ou supprime le préambule Markdown avant le premier titre Markdown reconnu (ATX ou Setext), avec contrôle CAS obligatoire.
- `patch_note_section` : remplace le contenu sous un heading existant avec contrôle CAS.
- `delete_note_section` : supprime explicitement une section H2-H6 (ATX ou Setext) et son sous-arbre.
- `rename_note_section` : renomme uniquement le titre d'une section H2-H6 (ATX ou Setext).
- `revert_note` : restaure les octets exacts d'une version conservée dans l'historique.
- `apply_organization_manifest` : valide puis applique un bundle local adressé par contenu,
  après confirmation liée au pré-état exact admis de l'organisation.

Garanties :

- confinement strict des notes dans `DATACRON_WRITE_PATHS` ; les sources et cibles notes d'un batch
  d'organisation doivent aussi rester dans le `organization.scope` live inchangé et passer la
  politique live d'admission des notes, exclusions comprises
- deux cibles internes sous CAS exact pour un batch d'organisation : `.datacron/VAULT.yaml`,
  seulement pour modifier le mapping top-level `organization` sans changer `organization.scope`,
  et `.datacron/ulids.json`, seulement quand Datacron dérive la migration de clé imposée par un
  `move_replace_exact`
- overwrite atomique via fichier temporaire + `os.replace`
- historique adressé par contenu avant modification d'une note existante
- `reconcile()` synchrone après un write normal ; la disponibilité immédiate dans la recherche
  n'est garantie que si cette réconciliation réussit
- audit log local
- pour un manifeste d'organisation : transaction récupérable après crash et remplacement
  atomique de chaque fichier ; la visibilité simultanée de plusieurs chemins n'est pas garantie

Le mode concurrent multi-machines n'est pas supporté pour les écritures : garde une règle
single-writer sur le vault.

Pour `apply_organization_manifest`, arrête aussi les autres clients et serveurs Datacron pendant
la fenêtre de maintenance. Avant l'application, conserve hors du vault une sauvegarde exacte aux
octets et vérifiée des notes affectées et du répertoire `.datacron` complet jusqu'à ce que tous les
contrôles post-commit soient verts. Appelle d'abord `mode="validate"`, contrôle les hashes bornés retournés,
puis réutilise l'exact `confirmation_token` avec `mode="apply"`. Le token lie le manifeste et ses
payloads, toutes les notes Markdown admises dans `organization.scope`, la configuration exacte du
vault et les sidecars d'identité, ainsi que le rapport projeté. Il ne lie délibérément pas les
octets de notes sans rapport situées hors de `organization.scope`. Toute modification d'un
composant authentifié invalide la confirmation avant mutation. `history_mode=full` est requis dès
la validation. Si Datacron dérive un nettoyage de collisions de casse du sidecar, contrôle aussi
`identity_sidecar_case_canonicalization_count` et son SHA-256 content-free avant d'appliquer ; ces
deux preuves sont liées au token et conservées dans le reçu durable.
Une source existante de `replace_exact` ou `move_replace_exact` doit porter son `id` dans le
frontmatter ; une identité disponible uniquement dans le sidecar n'est pas prise en charge par ce
schéma v1. Si le batch a déjà atteint son commit durable mais que la réconciliation ou l'oracle
planner échoue, la réponse le dit explicitement (`committed_index_incomplete` ou
`committed_report_mismatch`) et le même appel peut être rejoué avec le même token.
Un blocage de batch d'organisation est rapporté par `datacron ops inspect` avec une raison
`pending_batch_` et les deux réparations limitées à une note indisponibles ; applique alors le
rollback hors ligne complet du guide de santé opérationnelle, sans réparer ni isoler un seul membre.

## Fonctions disponibles

Datacron indexe un dossier de notes Markdown, expose un serveur MCP local, puis renvoie
au client les notes ou chunks pertinents au lieu d'un dump complet. Le vault reste un
dossier Markdown normal : Datacron ajoute seulement un sidecar `.datacron/` pour l'index,
les logs, les ULID internes, l'historique et le journal d'opérations.

| Surface | État actuel |
|---|---|
| Lecture vault | `list_notes`, `get_note`, resources `datacron://vault/map`, `vault/info`, `policy/active` |
| Recherche | SQLite FTS5/BM25, query-expansion FR↔EN, re-rank temporel, `ripgrep` via `search_regex` |
| Graphe local | Wikilinks et backlinks via `get_backlinks` |
| Écriture | 8 tools de note + 1 lot d'organisation, confinés et journalisés, désactivés par défaut sans `DATACRON_WRITE_PATHS` |
| Transport MCP | SDK Python MCP v2 via `MCPServer`, stdio local uniquement ; protocole moderne `2026-07-28` et compatibilité legacy `2025-11-25`, sans listener HTTP |
| Index | `datacron index` incrémental, `datacron reindex` complet, réparation conditionnelle à la lecture |
| Organisation | Bloc `organization` facultatif dans `VAULT.yaml` ; `datacron reorganize --dry-run` mesure l'écart en lecture seule, `apply_organization_manifest` applique |
| Évaluation | `datacron eval` sur le pipeline MCP réel : recall@k, MRR, nDCG, fraîcheur, latence et payload tokens |
| Setup guidé | `datacron setup` : init + index + enregistrement MCP en une commande |
| Clients | Auto-détection et enregistrement via `datacron setup --client all` : Claude Desktop, Claude Code, Cursor, Gemini CLI, Antigravity, LM Studio, Codex CLI, Windsurf, VS Code |
| Mémoire quotidienne | `session_context`, `prepare_follow_up`, `get_follow_up` : contexte borné, suivis sourcés et états structurés |
| Protocole mémoire | Contrat commun versionné pour le serveur et les clients ; `protocol status` vérifie sa distribution, pas le comportement du modèle |
| Distribution | Installeur Windows (`Datacron-Setup.exe`), exécutable autonome (PyInstaller) sans Python requis, ou installation depuis les sources |

## MCP Tools

### Lecture

| Tool | Description |
|---|---|
| `session_context` | Contexte initial borné et protocole commun versionné. |
| `prepare_follow_up` | Prépare les suivis sourcés sans écrire. |
| `get_follow_up` | Dernières révisions des suivis structurés. |
| `list_notes` | retourne une liste paginée, filtrable par dossier, tags et paires frontmatter clé/valeur, avec ULID, titre, tags, alias et dates |
| `get_note` | lit une note par ULID, chunk id ou chemin relatif, en contenu paginé, chunk ou plan de headings |
| `search_text` | effectue une recherche BM25 sur l'index FTS5 avec snippets classés et notes obsolètes démotées par défaut |
| `search_regex` | effectue une recherche regex via ripgrep et résout les lignes trouvées vers les chunks indexés |
| `get_backlinks` | retourne les chunks dont les wikilinks ciblent un ULID ou un alias résolu |

### Écriture

| Tool | Description |
|---|---|
| `create_note_ai` | crée une nouvelle note `_memory` typée, confinée aux chemins autorisés, sans overwrite et avec journal durable |
| `append_journal` | ajoute une entrée Markdown sous un heading, avec confinement, historique exact et écriture atomique |
| `set_frontmatter` | modifie uniquement les champs de cycle de vie, la liste `rejected` et la date `updated`, en préservant le corps Markdown |
| `patch_note_preamble` | remplace ou supprime le préambule avant le premier titre Markdown reconnu (ATX ou Setext), avec CAS obligatoire et préservation du suffixe |
| `patch_note_section` | remplace le contenu d'un heading existant avec CAS, historique exact et préservation des autres sections |
| `delete_note_section` | supprime explicitement une section H2-H6 (ATX ou Setext) et son sous-arbre, avec CAS optionnel et historique exact |
| `rename_note_section` | renomme le titre d'une section H2-H6 (ATX ou Setext) sans modifier son contenu ni son sous-arbre |
| `revert_note` | restaure une note depuis son historique adressé par contenu ; l'opération reste durable, réversible et auditée |
| `apply_organization_manifest` | valide un bundle local content-addressed contenant au moins une opération exacte sur une note et/ou un remplacement exact de la configuration `organization`, puis applique ses membres déclarés et, si nécessaire, la migration dérivée du sidecar ULID sous CAS ; l'application est journalisée et récupérable après crash |

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

<details>
<summary>Mesures historiques de recherche — 17 juillet 2026</summary>

Ces mesures portent sur un jeu de 19 questions et une configuration précise. Elles ne
constituent pas un benchmark de la version courante ni une garantie sur un autre vault.

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

Sur ce jeu historique, le recall@5 du tool atteignait celui du store BM25. Pour mesurer
le comportement sur tes propres notes, utilise `datacron eval` et un jeu de questions adapté.

</details>

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
datacron setup --protocol           # installe aussi les règles mémoire des clients
datacron protocol install --client all
datacron protocol status --client all --scope user
datacron init /path/to/vault
datacron status --vault /path/to/vault
datacron index --vault /path/to/vault
datacron reindex --vault /path/to/vault
datacron scrub-init --vault /path/to/vault
datacron scrub --vault /path/to/vault
datacron reorganize --vault /path/to/vault --dry-run          # mesure l'organisation, lecture seule
datacron reorganize --vault /path/to/vault --dry-run --json   # rapport machine stable
datacron eval --questions examples/eval-questions.example.yaml --vault /path/to/vault
datacron eval --questions local/golden.yaml --vault /path/to/vault --save-baseline
datacron eval --questions local/golden.yaml --vault /path/to/vault --compare --json
datacron mcp serve --vault /path/to/vault
datacron mcp install --client claude-desktop --vault /path/to/vault  # dédié Claude Desktop
datacron unregister --client all --scope both --vault /path/to/vault
datacron protocol uninstall --client all
```

## Limites actuelles

- Recherche lexicale : pas de recherche vectorielle ni d’embeddings.
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
- [Utiliser Datacron avec Ollama](docs/fr/ollama.md)
- [Questions fréquentes](docs/fr/faq.md)
- [Guide utilisateur](docs/fr/user-guide.md)
- [Mémoire quotidienne, personnes et engagements](docs/fr/memory-discipline.md)

Références techniques :

- [Conventions du vault (SPEC)](docs/fr/spec.md)
- [Organisation du vault](docs/fr/organization.md)
- [Architecture et surface publique](docs/fr/architecture.md)
- [Frontière de sécurité](docs/fr/security-boundary.md)
- [Scrubber d'intégrité](docs/fr/integrity-scrubber.md)
- [Santé opérationnelle et durabilité](docs/fr/operational-health.md)
- [Contrat de fraîcheur](docs/fr/freshness-contract-v1.md)

## Développement

La CI exécute les invariants et toute la suite de régression sur Linux/Python 3.12 pour les changements limités aux README, au CHANGELOG et aux pages Markdown de `docs/fr/` ou `docs/en/`. Tout autre changement conserve les six combinaisons Linux/Windows et Python 3.11–3.13. Les publications imposent la matrice complète ; un diff vide ou invérifiable aussi. ShellCheck, l’audit des dépendances et le contrôle obligatoire `Quality gate` restent actifs dans les deux parcours.

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

[Écritures fiables et contrôles qualité](docs/fr/improvements.md)
