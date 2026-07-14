# Datacron - Décisions tranchées v2.1

**Français** · [English](../en/decisions-v2.1.md)

> **Statut** : Arbitrage final post cross-review
> **Auteur** : Julien Bombled (synthèse arbitrée par Claude)
> **Date** : 2026-05-17
> **Sources de cross-review** :
> - Archives locales non versionnées sous `local/docs-ai/`
> - Vérification web Anthropic docs (Cowork = remote MCP only)
> **Remplace** : ARCHITECTURE.md v2.0 (qui devient v2.1 après patch)

---

## 1. Pourquoi v2.1

La v2.0 (architecture Claude post-pivot) a été soumise à une cross-review par **Gemini Pro et ChatGPT 5.5 Pro** avec un prompt commun structuré (12 décisions à challenger + format de sortie strict).

Les deux modèles ont produit des verdicts indépendants. Cette v2.1 est le **résultat d'arbitrage** :

- Convergences fortes (10+ sur 12 décisions) → on tranche dans le sens de la convergence sans hésiter
- Insights uniques utiles → on les intègre individuellement avec justification
- Une vérification empirique critique (Cowork = remote MCP) a été menée via web search Anthropic docs
- Le contrarian take de ChatGPT (DVS pas marketé comme spec ouverte) a été retenu

---

## 2. Le facteur décisif : Cowork = remote MCP only

**ChatGPT a posé un risque que Claude n'avait pas vu** : *Cowork et claude.ai ne supportent que les MCP connectors **remote** brokered par l'infrastructure Anthropic - pas les serveurs MCP **locaux** en stdio.*

**Vérification empirique faite** (Anthropic Help Center, 2026-05-17) :

> "Local MCP servers configured in Claude Desktop via claude_desktop_config.json are a separate mechanism and do use your local network, but those aren't available in Cowork or claude.ai."
>
> "For remote connectors used with Cowork, your MCP server must be reachable over the public internet from Anthropic's IP ranges."

**Conséquence pour Datacron** :

| Client | Mode | Compatible Datacron v1 ? |
|---|---|---|
| Claude Desktop | local stdio | ✅ direct |
| Claude Code | local stdio | ✅ direct |
| Cowork | remote HTTPS only | ⚠️ tunnel requis (v1.x) |
| claude.ai web | remote HTTPS only | ⚠️ tunnel requis (v1.x) |
| Mobile apps | remote HTTPS only | ⚠️ tunnel requis (v1.x) |
| Cursor | local stdio | 🟡 à valider v1.1 |
| ChatGPT Desktop, Gemini | varie | roadmap v2 |

**Arbitrage Julien** : v1 = **Claude Desktop + Claude Code uniquement**. Cowork via tunnel HTTPS sécurisé (Cloudflare Tunnel intégré) en **v1.x**, avec documentation honnête du trade-off local-first.

---

## 3. Table comparative des 12 décisions

| # | Décision v2.0 | Gemini | ChatGPT | **Arbitrage v2.1** |
|---|---|---|---|---|
| 1 | MCP server hero | ✅ | 🟡 | ✅ - **"local context router read-only" pour MVP** |
| 2 | DVS spec | ⚠️ | 🟡 | 🔄 - **DVS comme overlay**, pas marketé comme open spec |
| 3 | L0-L5 trust model | 🟡 (3) | 🟡 (3 UX) | 🔄 - **3 niveaux UX visible**, L0-L5 dans backend pour évolutivité |
| 4 | FastMCP custom | ✅ | ✅ | ✅ - confirmé |
| 5 | LangGraph optional | ❌ | ⚠️ | 🔄 - **dropped entirely** du MVP, post-v2 |
| 6 | Retrieval stack | 🟡 | ⚠️ | 🔄 - **ripgrep + SQLite FTS5 uniquement v1**, embeddings après eval |
| 7 | Prompt injection | ❌ classifier | 🟡 | 🔄 - **sandbox delimiters seul**, focus tool-layer security |
| 8 | Multi-client | 🟡 (Claude+Cursor) | ❌ | 🔄 - **Claude Desktop+Code only v1**, Cursor v1.1 |
| 9 | Monorepo 5 packages | ✅ | 🟡 | 🔄 - **1 seul package Python** v1, monorepo ouvert pour Tauri stub |
| 10 | 4 canaux distribution | ⚠️ | ⚠️ | 🔄 - **PyPI/pipx uniquement v1**, brew v1.1 |
| 11 | 8 phases 20 semaines | ❌ | ❌ | 🔄 - **MVP 4 semaines read-only**, reste débloqué par usage |
| 12 | Multi-machine sync | hands-off | single-writer | 🔄 - **single-writer rule v1**, autres patterns "unsupported" documentés |

**Score** : 11 pivots sur 12 décisions. La v2.0 était trop ambitieuse. La v2.1 est exécutable.

---

## 4. Décisions arbitrées - détail motivé

### 4.1 MCP server = local context router read-only

**v2.0** : "le hero du projet", expose tools de lecture ET d'écriture.

**v2.1** : Le hero **read-only** pour v1. Les write tools (`create_*`, `patch_*`, `delete_*`) sont **reportés post-v1** pour deux raisons cumulatives :

- *Concurrence/file lock* (Gemini risk #1) : patcher un fichier ouvert dans Obsidian/VSCode = corruption. Pas de stratégie de file-locking robuste dans le MVP.
- *HITL délégué aux clients* (ChatGPT risk #2) : on n'a aucune garantie que Claude Desktop affiche bien un diff viewer ou une typed confirmation pour les writes L3+. Tant que Datacron ne *possède* pas l'UX d'approbation, on ne donne pas les ciseaux à l'IA.

**Impact** : Phase 0 + Phase 1 = MVP complet. Phase 3 (write path) attend une décision séparée après l'usage réel du read-only.

### 4.2 DVS comme overlay (pas une spec marketée)

**v2.0** : SPEC.md DVS v1.0 normative, dossiers réservés hardcoded (`_inbox/`, `_ai_generated/`, `_journal/agent/`), frontmatter requis.

**v2.1** :

- Datacron **lit n'importe quel vault Markdown sans migration**. Aucune note existante n'est forcée à se conformer à DVS.
- Les `id` ULID, `content_hash`, et métadonnées Datacron sont stockés en **side-metadata dans `.datacron/`**, pas dans le frontmatter des notes existantes.
- DVS frontmatter n'est écrit **que sur les notes que Datacron crée**.
- Aucune commande de normalisation n'est livrée ; le vault existant reste inchangé.
- Les "dossiers réservés" deviennent **configurables** dans `.datacron/VAULT.yaml` - l'utilisateur peut mapper `_inbox/` sur son propre `00-Inbox/` PARA s'il le souhaite.
- **DVS n'est pas marketé comme une "open spec"** (contrarian take ChatGPT, retenu). Le fichier `SPEC.md` reste documentation interne de référence. Si la demande communautaire émerge, on l'extraira en `jbombled/datacron-spec` plus tard.

**Impact** : 
- README.md retire la section "Le contrat ouvert : Datacron Vault Specification" et la remplace par une mention sobre.
- SPEC.md devient `docs/dvs-reference.md` (interne).
- Adoption Datacron sur vault existant = **zéro friction**.

### 4.3 Trust model - 3 niveaux UX visible, L0-L5 backend

**v2.0** : 6 niveaux L0-L5 exposés à l'utilisateur.

**v2.1** : Backend conserve 6 niveaux pour évolutivité, UX montre **3 catégories** :

| Catégorie UX | Niveaux backend | Validation |
|---|---|---|
| **auto-create** | L0, L1 | Pas de friction |
| **review-patch** | L2, L3 | Diff visuel + approve |
| **dangerous** | L4, L5 | Double confirmation + Git tag |

L'utilisateur configure dans Studio/CLI à ces 3 niveaux. Le moteur de policies en interne mappe vers les 6 niveaux fins. Forward-compat préservée.

**Note** : Tant que v1 est read-only, ces niveaux ne sont pas exposés du tout. Ils arrivent avec les write tools post-v1.

### 4.4 FastMCP custom - confirmé

Convergence absolue Gemini ✅ + ChatGPT ✅. Pas de débat. Custom est nécessaire pour :
- Direct filesystem access (vs REST API Obsidian qui requiert l'app)
- DVS-lite overlay
- Write policies fines (quand on les ajoutera)
- Audit logging
- Path confinement strict
- Indépendance éditeur

### 4.5 LangGraph dropped entirely

**v2.0** : LangGraph en "optionnel" pour mode offline et tâches autonomes (Phase 4).

**v2.1** : **Hors MVP entièrement**. Justifications cumulées des deux reviews :

- "Optionnel" = quand même surface dependency, design coupling, docs, tests (ChatGPT)
- Mode offline est un produit différent avec besoins différents (Gemini)
- Cron jobs déterministes en CLI suffisent pour synthèse hebdo (ChatGPT)

**Si** un jour on a besoin d'orchestration agentique offline, on évaluera à ce moment-là - LangGraph, PydanticAI, ou rien.

**Impact** : Phase 4 supprimée. `datacron-agent/` ne sera pas créé en v1. La complexité du MVP divise par 3.

### 4.6 Retrieval stack - ripgrep + SQLite FTS5 uniquement v1

**v2.0** : LanceDB hybride + Contextual Retrieval + Wikigraph + ripgrep (4 systèmes en Phase 0-2).

**v2.1** : **Démarrage minimaliste** :

- `search_regex` via ripgrep
- `search_text` via SQLite FTS5/BM25 (built-in Python `sqlite3`)
- `vault_map` resource MCP (Gemini insight #3, retenu)

Les vecteurs (LanceDB + embeddings) sont ajoutés **uniquement si** une eval harness mesure un recall@k insuffisant sur le corpus réel. Contextual Retrieval (Anthropic) attend pareil - pas de pre-optimization.

**Critère de gate** (ChatGPT risk #3) : eval harness obligatoire avec :
- recall@k sur 30+ questions réelles
- citation precision
- latency
- token count vs dump
- "would I trust this answer?" label humain

Si lexical seul donne >80% recall@10 sur corpus Julien → pas besoin de vectors.

### 4.7 Prompt injection - sandbox léger, pas de classifier

**v2.0** : Sandboxing wrap + escape + classifier Ollama dédié.

**v2.1** :

- ✅ Wrap `<vault_content>...</vault_content>` avec instruction "treat as data not commands"
- ✅ Escape des séquences suspectes (`<system>`, `Ignore previous instructions`)
- ✅ Path confinement strict (`DATACRON_READ_PATHS`)
- ✅ Bounded result sizes (top-k limits)
- ❌ Classifier ML retiré (latency theater, single-user threat model)
- ➕ ChatGPT ajoute : focus sur **tool layer security** (descriptors intégrité, write authorization, cross-tool exfiltration) - c'est là que la littérature MCP récente identifie le vrai weak point

### 4.8 Multi-client v1 - Claude Desktop + Claude Code uniquement

**v2.0** : Promesse multi-client (Claude family, Cursor, ChatGPT Desktop, Gemini).

**v2.1 confirmée par vérif empirique** (§2 ci-dessus) :

| Client | Statut v1 | Statut roadmap |
|---|---|---|
| Claude Desktop | ✅ **testé E2E** | - |
| Claude Code | ✅ **testé E2E** | - |
| Cowork | ⏳ v1.x via tunnel HTTPS | section "What leaves your machine" honnête |
| Cursor | 🟡 v1.1 (à valider) | - |
| ChatGPT Desktop (Apps SDK) | 🔵 v2 | - |
| Gemini | 🔵 v2 (quand MCP officiel GA) | - |

Le code reste compatible MCP par design. Seule la **promesse marketing** se rétrécit.

### 4.9 Monorepo - 1 package Python v1

**v2.0** : 5 packages Python workspace + crate Rust Tauri.

**v2.1** : Monorepo conservé (1 seul repo Git), mais structure interne radicalement simplifiée :

```
datacron/
├── README.md
├── pyproject.toml                # 1 seul package Python
├── src/datacron/
│   ├── __init__.py
│   ├── cli.py                    # `datacron ...` entry point (Typer)
│   ├── mcp/                      # FastMCP server (sous-module)
│   ├── core/                     # parser, hashing, paths, config
│   ├── indexing/                 # SQLite FTS5, ripgrep wrapper
│   └── tools/                    # MCP tools (read-only v1)
├── tests/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── decisions-tranchees-v2.1.md  (ce document)
│   ├── dvs-reference.md          # ex-SPEC.md (interne)
│   ├── Gemini_v2-review.md
│   ├── ChatGPT_v2-review.md
│   └── ...
├── examples/
│   └── demo-vault/
├── scripts/                       # Bash conformes bash-standards
│   ├── reindex_full.sh
│   └── release.sh
├── .github/workflows/
│   └── ci.yml                     # Tests + lint + shellcheck
└── LICENSE                        # Apache 2.0
```

Le repo reste prêt à accueillir `crates/datacron-studio/` quand on construira Studio, et un éclatement en multi-packages quand on aura ≥3 sous-systèmes vraiment distincts (Agent, Daemon, etc.).

### 4.10 Distribution - PyPI/pipx uniquement v1

**v2.0** : PyPI + Homebrew + Docker + Tauri binaries.

**v2.1** : **PyPI/pipx uniquement** pour v1.

- ✅ `pipx install datacron` - un seul canal officiel
- 📅 Homebrew tap → v1.1 si retours macOS demandent
- 📅 Docker → CI/demo seulement, pas distribué (uid/gid hell pour file-local)
- 📅 Tauri binaries → reportés à Studio v2 si demande

> **Addendum (ADR-017, 2026-07-14)** : cette décision est révisée - un exécutable autonome
> (PyInstaller) est ajouté **en complément** de PyPI/pipx, pour les utilisateurs sans Python.
> PyPI/pipx reste le canal recommandé pour les environnements Python. Détails dans
> [architecture.md](architecture.md).

### 4.11 Roadmap - MVP 4 semaines read-only

**v2.0** : 8 phases ~20 semaines.

**v2.1** : **Phase 0 seulement** = MVP de 4 semaines. Tout le reste devient post-MVP, débloqué par usage réel.

**Phase 0 (4 semaines)** - *"Local context router read-only"* :

| Sem | Livrable |
|---|---|
| 1 | `pyproject.toml`, headers Apache 2.0, FileLogger Python, parser frontmatter, paths confinement, `datacron init`, `datacron status` |
| 2 | FastMCP server stdio, tools `list_notes`, `get_note`, resource `vault_map` |
| 3 | SQLite FTS5 indexer, `search_text`, ripgrep wrapper, `search_regex`, `datacron index`, `datacron reindex` |
| 4 | `datacron mcp install --client claude-desktop`, eval harness, dogfooding sur vault Julien, polish, release `datacron 0.1.0` sur PyPI |

**Critère de succès Phase 0** (ChatGPT, mot pour mot) : *"From Claude Desktop, ask 30 real questions against Julien's vault and beat manual folder dumping on answer quality, latency, and token cost."*

**Post-MVP** (débloqué par usage v0.1.0) :
- v0.2 : write tools (`append_journal`, `create_draft_note`) + Git snapshot
- v0.3 : tunnel HTTPS futur pour Cowork, sans commande livrée à ce jour
- v0.4 : embeddings + LanceDB si eval Phase 0 montre besoin
- v0.5 : Contextual Retrieval si eval v0.4 montre encore un gap
- v1.0 : stabilisation + docs MkDocs + Homebrew tap
- v2.0+ : Studio Tauri, LangGraph offline, multi-client Cursor/ChatGPT/Gemini

### 4.12 Sync - single-writer vault rule v1

**v2.0** : "Hands-off, l'utilisateur gère Git/Syncthing/iCloud".

**v2.1** (ChatGPT pivot) :

- **v1** : Datacron écrit depuis **une seule machine**. Toutes les autres machines sont en mode read-only (ou n'utilisent pas Datacron).
- Tout autre pattern (Datacron writer sur 2 machines avec Syncthing entre les deux) est documenté comme **explicitement non supporté**, car ça brise `content_hash_before`, l'index freshness, et l'audit log.
- Git reste pour rollback uniquement, pas pour sync distribué.

---

## 5. Insights uniques retenus

| Source | Insight | Statut |
|---|---|---|
| Gemini | Concurrence/file-lock corruption | ✅ → write tools reportés post-v1 |
| Gemini | Vault Map dans system prompt | ✅ → resource MCP `datacron://vault/map` (MVP) |
| Gemini | Filesystem as state machine (pas YAML `ai.status`) | ✅ → `_drafts/` au lieu de status YAML (v0.2+) |
| Gemini | `datacron reindex` explicite vs watcher parfait | ✅ → commande MVP |
| ChatGPT | Eval harness obligatoire avant tout ajout retrieval | ✅ → gate dans Phase 0 |
| ChatGPT | HITL owned par Datacron (TUI/CLI) | ⏸ → v0.2 quand write arrive |
| ChatGPT | Single-writer vault rule | ✅ → documenté v1 |
| ChatGPT | Cowork = remote MCP **(vérifié empiriquement)** | ✅ → cadrage v1 ajusté |
| ChatGPT | DVS pas marketé comme open spec | ✅ → contrarian retenu |
| ChatGPT | Tool-layer security (descriptors, exfiltration) | ✅ → focus v0.2 quand write arrive |

---

## 6. Insights non retenus, avec justification

| Source | Insight | Pourquoi non retenu |
|---|---|---|
| Gemini | "Local-first est une illusion" (contrarian) | Position polémique partiellement juste mais Datacron *peut* être strictement local-first si l'utilisateur reste sur Ollama. Le pitch est conservé mais **clarifié honnêtement** : section "What leaves your machine" dans le README qui explique ce qui part chez Anthropic quand on utilise Claude API. Pas mentir, pas se gargariser de buzzword. |
| Gemini | "Use Obsidian's standards directly, no DVS" | Obsidian n'a pas de convention pour `origin: ai`, audit, ou trust. DVS reste utile *en tant qu'overlay invisible*, juste pas marketé. |
| ChatGPT | Cowork "marked unsupported until working E2E exists" | Plus dur que nécessaire. Position v2.1 : v1.x via tunnel HTTPS avec docs honnêtes du trade-off, plutôt que "unsupported" total. |

---

## 7. Le MVP 4 semaines - spec exécutable

### Surface technique exposée

```
Tools MCP (read-only, MVP) :
  • list_notes(folder?, tags?, limit?) → array of {path, title, frontmatter}
  • get_note(id_or_path, format=full|map) → full content OR document-map
  • search_text(query, limit=20) → array of {chunk, path, score}      [SQLite FTS5/BM25]
  • search_regex(pattern, glob?, limit=20) → array of matches         [ripgrep wrapper]
  • get_backlinks(target) → array of source paths                     [wikilinks parser]

Resources MCP :
  • datacron://vault/map     → folder/file tree, lightweight (~2k tokens max)
  • datacron://vault/info    → vault metadata (path, note count, last index)
  • datacron://policy/active → current policy (empty/permissive in MVP)

CLI :
  • datacron init <path>             → initialize .datacron/ in any markdown folder
  • datacron status                  → show vault state, index freshness
  • datacron index                   → build/rebuild index
  • datacron reindex                 → force full reindex
  • datacron mcp serve               → start FastMCP server (stdio)
  • datacron mcp install --client claude-desktop → write client config
  • datacron eval                    → run eval harness on test questions
```

### Garde-fous techniques

- Path confinement strict via `DATACRON_READ_PATHS`
- Bounded result sizes : `maxMatchesPerHit=20`, content truncation if > 8k tokens
- Sandboxing `<vault_content>...</vault_content>` wrapping
- Deterministic chunk IDs (ULID + header_path + ordinal)
- Headers Apache 2.0 sur tout fichier `.py` (dev-standards)
- Shellcheck clean sur tout `.sh` (bash-standards)
- FileLogger Python (`~/.datacron/logs/datacron_{YYYYMMDD}.log`)

### Critère de succès

Sur le vault personnel de Julien, **30 questions réelles** posées via Claude Desktop avec Datacron MCP doivent battre le baseline (folder dump) sur :
- ✅ Qualité de réponse (label humain "would I trust this?")
- ✅ Latence (P50 < 5s, P95 < 12s)
- ✅ Token cost (réduction ≥10× vs dump équivalent)
- ✅ Citation precision (au moins 1 chunk citable par réponse non-triviale)

Si ces 4 critères passent → release v0.1.0 PyPI. Sinon → itération.

---

## 8. Conformité aux standards

Inchangé v2.0 → v2.1 :
- Python : Apache 2.0 headers, English, zero hardcoding, FileLogger, ruff + mypy strict, async/await, no `try/except: pass`.
- Bash : shebang `env bash`, `set -euo pipefail`, logging, dry-run, trap, prereqs, getopts, `--help`, shellcheck clean.

---

## 9. Documents à patcher après ce doc

| Fichier | Action | Statut |
|---|---|---|
| `README.md` | Patch v2.1 : multi-client narrow, pas de Studio v1, "What leaves your machine" honnête, DVS overlay | À faire |
| `SPEC.md` | Renommer `docs/dvs-reference.md`, simplifier (overlay, no migration, filesystem state machine) | À faire |
| `docs/ARCHITECTURE.md` | Patch v2.1 : MVP 4 sem, no LangGraph, no embeddings v1, no write tools v1, no Tauri, 1 package | À faire |
| `docs/architecture-overview.svg` | Update : Cowork retiré du top layer, Studio retiré, LangGraph retiré du MVP | À faire |

---

## 10. Méthodologie - ce qu'on retient pour le futur

Cette boucle **design → cross-review → arbitrage → spec exécutable** a produit en 24h une spec plus solide qu'un mois de design en silo. À refaire à chaque pivot majeur :

1. Produire un design v0 défendable et structuré.
2. Soumettre à **deux modèles indépendants** avec prompt commun strict et format de sortie identique.
3. Lire les deux retours **avec humilité** - chercher activement les points où ils ont raison, pas les défendre.
4. Identifier les **convergences fortes** (= signaux clairs) et les **insights uniques** (= valeur ajoutée).
5. **Vérifier empiriquement** les claims critiques (ici : Cowork = remote, via web search Anthropic docs).
6. Arbitrer chaque point avec justification motivée.
7. Re-spec en éliminant **tout ce qui n'est pas dans le MVP**.

Coût : ~2h de prompt engineering + 1h de lecture/synthèse + 1h de rédaction. Gain : pivots évités, MVP 4 sem au lieu de 20 sem, scope creep éliminé.

---

*Document v2.1 figé le 2026-05-17. Toute décision peut être réouverte via un nouvel ADR daté, mais le seuil de réouverture est élevé : convergence forte des deux reviews + vérification empirique constituent une base solide.*
