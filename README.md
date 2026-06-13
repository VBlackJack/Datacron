# Datacron

> **Un pont MCP local-first qui rend ton vault Markdown interrogeable par Claude - sans dump et sans cloud.**
> Au lieu de coller 50 notes dans le contexte de Claude (50 000 tokens), Datacron répond
> aux requêtes MCP de Claude Desktop ou Claude Code en lui envoyant 5 chunks pertinents
> (1 000 tokens). Économie typique : 20-50×. Tes notes restent sur disque, lisibles dans
> n'importe quel éditeur Markdown.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Status: design v2.1](https://img.shields.io/badge/Status-design_v2.1-orange)](docs/decisions-tranchees-v2.1.md)
[![MCP: Claude Desktop · Claude Code](https://img.shields.io/badge/MCP-Claude_Desktop_·_Code-purple)](#works-with)

---

## Trois promesses, trois lignes rouges

| Promesse | Ligne rouge |
|---|---|
| **💸 Économie de tokens** - chunks pertinents au lieu de dumps | Toujours via MCP, jamais en dump brut |
| **📂 Vault portable** - fonctionne sur tes notes existantes sans migration | Pas de format imposé. Datacron lit ce qu'il y a |
| **🔒 Local-first transparent** - tes notes ne quittent jamais ta machine pour Datacron | Section *[What leaves your machine](#what-leaves-your-machine)* honnête, pas de buzzword |

---

## Works with

Datacron est un **serveur MCP local** (stdio). En v1 il est testé sur :

| Client | Statut | Mode |
|---|---|---|
| **Claude Desktop** | ✅ v1 | Local stdio natif. Config en 1 commande : `datacron mcp install --client claude-desktop` |
| **Claude Code** | ✅ v1 | Local stdio natif. `claude mcp add datacron ...` |
| **Cursor** | 🟡 v1.1 (à valider) | Local stdio (compatibilité MCP par design) |
| **Cowork / claude.ai / mobile** | ⏳ v1.x | Cowork ne supporte que les *remote* MCP connectors. Nécessite un tunnel HTTPS - voir [§Cowork](#et-cowork-) |
| **ChatGPT Desktop / Gemini** | 🔵 v2 | Quand leurs hosts MCP seront stables |

Datacron parle MCP standard JSON-RPC. Il fonctionnera avec tout futur client MCP, mais on
ne *promet* pas ce qu'on n'a pas testé.

### Et Cowork ?

Cowork (et claude.ai) ne supportent pas les serveurs MCP en local stdio - seulement des
*remote connectors* accessibles publiquement depuis l'infrastructure Anthropic. Conséquence :
pour utiliser Datacron depuis Cowork, il faut exposer ton serveur local via un **tunnel HTTPS**
(Cloudflare Tunnel, Tailscale Funnel, ngrok...).

C'est techniquement faisable, sécurisable avec auth token, mais ça change le profil de risque
("local-first" devient "local serveur exposé via tunnel chiffré"). Datacron **v1.x** intégrera
une commande `datacron mcp serve --remote` qui orchestrera ce tunnel avec auth + logs d'accès,
et documentera honnêtement le trade-off. En attendant, **utilise Claude Desktop ou Claude Code**
pour tirer parti de Datacron.

---

## Pourquoi Datacron (l'argument-massue)

**Avant Datacron**, tu demandes à Claude "résume mes notes sur X" :
- Tu colles 50 notes dans le contexte → ~50 000 tokens
- Claude lit tout, dont 95% non pertinent
- Tu paies 50× plus que nécessaire, le contexte est saturé

**Avec Datacron**, le même prompt :
- Claude appelle `search_text("X")` via MCP
- Datacron renvoie 5 chunks pertinents (~1 000 tokens)
- Claude répond avec citations cliquables
- **Facteur 50× d'économie**, qualité maintenue, contexte préservé pour la suite

---

## Ce que Datacron v1 fait (et ne fait pas)

**v1 (MVP 4 semaines, read-only)** :

| Use case | Comment |
|---|---|
| 💬 **Q&A sur ton vault depuis Claude Desktop** | Claude appelle `search_text` / `search_regex` / `get_note` selon ce que tu demandes |
| 🗺️ **Vault map injecté à Claude** | Resource MCP `datacron://vault/map` donne à Claude la structure globale en ~2k tokens |
| 🔍 **Recherche lexicale + regex** | SQLite FTS5/BM25 + ripgrep wrapper |
| 🔗 **Backlinks et wikilinks** | `get_backlinks` pour le graphe de liens |

**v1 ne fait PAS** (reporté post-MVP, par décision motivée - voir [decisions-tranchees-v2.1.md](docs/decisions-tranchees-v2.1.md)) :

- ❌ Écriture dans le vault (write tools post-v0.2 quand on aura validé l'UX d'approbation)
- ❌ Embeddings vectoriels (ajoutés seulement si l'eval mesure que le lexical seul est insuffisant)
- ❌ Agent autonome / LangGraph (Claude orchestre, c'est suffisant)
- ❌ Studio GUI Tauri (ligne de commande suffit pour le MVP)
- ❌ Mode multi-machines avec writes concurrents (single-writer rule en v1)

---

## What leaves your machine

Datacron lui-même **n'envoie rien** à un serveur tiers. Pas de télémétrie, pas d'analytics,
pas de crash reporter cloud.

**Mais** : quand tu utilises Claude Desktop avec Datacron, c'est **Claude Desktop** qui envoie
à Anthropic les chunks que Datacron lui renvoie via MCP. C'est par design - c'est comme ça que
fonctionne tout client MCP cloud.

Ce qui veut dire :
- ✅ **Le vault entier reste local.** Anthropic ne voit jamais ton vault complet.
- ✅ **Seuls les chunks pertinents partent** (typiquement 5-10 chunks par requête).
- ✅ **Tu contrôles** ce qui part en choisissant ta requête.
- ⚠️ **Les chunks transitent quand même par Anthropic.** Si tu veux du strict air-gapped, n'utilise pas Claude - c'est cohérent, pas Datacron qui décide.

Si tu veux du 100% local, Datacron v2 ajoutera un mode `datacron ask --local` qui utilise
Ollama localement, mais ça sort du scope du MVP read-only.

---

## Installation (v1 MVP)

### Prérequis

- Python 3.11+
- `ripgrep` installé (`brew install ripgrep` / `apt install ripgrep` / `choco install ripgrep`)
- Un vault Markdown quelque part sur ton disque
- Claude Desktop ou Claude Code

### Installation

```bash
# Installer Datacron
pipx install datacron

# Initialiser sur ton vault existant (ne touche à rien, crée .datacron/)
datacron init ~/Notes

# Construire l'index
datacron index

# Câbler à Claude Desktop
datacron mcp install --client claude-desktop

# Vérifier
datacron status
```

Redémarre Claude Desktop, puis pose ta question dans une conversation :

> *"Datacron, qu'est-ce que j'ai écrit récemment sur Kafka ?"*

Claude appellera Datacron en arrière-plan, recevra les chunks pertinents, te répondra avec
citations. Tu peux cliquer sur les citations pour ouvrir les notes correspondantes.

> **Indexation incrémentale.** `datacron index` ne réindexe que les notes modifiées
> (comparaison `content_hash`, court-circuit par `mtime`) et supprime les notes disparues -
> relancer la commande sur un vault inchangé est quasi instantané. L'index se répare aussi
> seul au premier `search_*` suivant une édition : pas besoin de réindexer manuellement après
> chaque modification (`datacron reindex` force une reconstruction complète si besoin).
>
> **Réglages** (variables d'environnement, optionnelles) :
> `DATACRON_CHUNK_MAX_TOKENS` (taille max d'un chunk, défaut `1024`),
> `DATACRON_GET_NOTE_MAX_TOKENS` (budget de `get_note(format=full)`, défaut `25000`),
> `DATACRON_MAX_RESULT_TOKENS` (budget des résultats de recherche, défaut `8000`).

---

## Démarrage rapide (5 minutes)

```bash
pipx install datacron
datacron init ~/Notes
datacron index
datacron mcp install --client claude-desktop
datacron status
```

Redémarre Claude Desktop. C'est fait.

---

## Architecture (en une image)

```
   ┌────────────────────────────────────────────────────────┐
   │  Claude Desktop  /  Claude Code  (v1)                  │
   └────────────────────────┬───────────────────────────────┘
                            │ MCP stdio (JSON-RPC)
                            │
   ┌────────────────────────▼───────────────────────────────┐
   │  Datacron MCP server  (FastMCP, Python, read-only v1)  │
   │  Tools: list_notes · get_note · search_text ·          │
   │         search_regex · get_backlinks                   │
   │  Resources: datacron://vault/map · /info · /policy     │
   └─────┬──────────────────────────────────┬───────────────┘
         │ filesystem                       │ reads index
         │                                  │
   ┌─────▼──────────────┐         ┌────────▼────────────────┐
   │  Ton vault         │         │  .datacron/             │
   │  Markdown          │         │  • SQLite FTS5 index    │
   │  (any structure)   │         │  • ULID side-metadata   │
   │                    │         │  • Logs                 │
   └────────────────────┘         └─────────────────────────┘
```

Pas d'agent autonome. Pas de LangGraph. Pas de Studio. Pas de Tauri.
**Juste un pont rapide entre Claude et ton filesystem, audité et sécurisé.**

[→ Architecture détaillée](docs/ARCHITECTURE.md) · [→ Décisions tranchées v2.1](docs/decisions-tranchees-v2.1.md)

---

## Comment Datacron traite ton vault

Datacron lit **n'importe quel vault Markdown sans migration**. Aucune note existante n'est
modifiée.

- Les notes existantes sont indexées telles quelles.
- Datacron crée un dossier `.datacron/` à la racine du vault (gitignorable) qui contient
  son index SQLite, ses logs, et ses métadonnées internes (`id` ULID stables, hashes).
- Tes notes peuvent suivre n'importe quelle structure : PARA, Zettelkasten, Johnny Decimal,
  ou rien du tout.
- Les wikilinks `[[note]]` sont reconnus, les frontmatter YAML sont parsés s'ils existent.

Une référence interne `docs/dvs-reference.md` documente le format optionnel utilisé quand
*Datacron lui-même* écrit des notes (post-v0.2), mais c'est une convention interne, pas un
standard que tu dois suivre.

---

## Vie privée & sécurité

- **Aucune télémétrie** - pas de phone-home, pas d'analytics tiers.
- **Pas de LLM cloud appelé par Datacron lui-même** - c'est le client MCP (Claude Desktop) qui décide ce qu'il envoie à Anthropic, pas Datacron.
- **Path confinement strict** - variables d'env `DATACRON_READ_PATHS` confinent physiquement le serveur.
- **Sandboxing des contenus** - le contenu des notes renvoyé via MCP est wrappé `<vault_content>...</vault_content>` avec instruction explicite au client de traiter comme données et non comme commandes.
- **Bounded results** - limites strictes sur la taille des retours pour éviter le context bloat.
- **Audit log local** - toutes les opérations MCP sont loguées en clair dans `~/.datacron/logs/`.

---

## Statut

Datacron est en **phase de design figée v2.1** - pas encore de code livré.

La spec exécutable est dans [decisions-tranchees-v2.1.md](docs/decisions-tranchees-v2.1.md).
Cette spec a été produite par une boucle design → cross-review (Gemini Pro + ChatGPT 5.5 Pro) →
arbitrage, avec vérification empirique du support MCP côté Anthropic.

**Phase 0 (MVP 4 semaines)** démarre prochainement. Critère de succès : 30 questions réelles
sur le vault, depuis Claude Desktop, qui battent le baseline "folder dump" en qualité, latence,
et coût tokens.

---

## Pourquoi pas ...

| Alternative | Pourquoi Datacron |
|---|---|
| **Notion AI / Mem.ai / Reflect** | Cloud-first, propriétaire, lock-in |
| **Obsidian Copilot, Smart Connections** | Couplé Obsidian uniquement, pas multi-éditeur |
| **AnythingLLM, Open WebUI** | Q&A sur documents, mais pas un pont MCP token-efficient avec Claude |
| **Coller le vault dans Claude** | Marche, mais 20-50× plus cher en tokens, contexte saturé |

---

## Contribuer

Datacron est sous licence **Apache 2.0**. Toute contribution respecte les standards de code
de Julien Bombled (Apache 2.0 headers, English code, zero hardcoding, FileLogger, shellcheck clean).

---

## Licence

Copyright 2026 Julien Bombled - Licensed under the [Apache License, Version 2.0](LICENSE).
