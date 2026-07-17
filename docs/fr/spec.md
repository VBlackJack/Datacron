# Datacron - Conventions internes du vault

**Français** | [English](../en/spec.md)

> **Statut** : v1.1 (référence de surcouche, pas un standard ouvert commercialisé)
> **Auteur** : Julien Bombled
> **Dernière mise à jour** : 2026-05-17
> **Licence** : [Apache 2.0](../../LICENSE)
> **Portée** : Ce document décrit les conventions internes que Datacron utilise quand il lit
> des métadonnées d'un vault et quand il écrit de nouvelles notes. Ce **n'est pas une spec
> normative que d'autres vaults devraient suivre**. Datacron lit n'importe quel dossier
> Markdown sans exiger de migration.

---

## 1. Philosophie de lecture - zéro migration

Datacron lit **n'importe quel dossier de fichiers Markdown** sans imposer de structure ni de
frontmatter. Le vault existant de l'utilisateur reste intact.

- Aucun champ de frontmatter requis sur les notes existantes.
- Aucune structure de dossiers requise.
- Les wikilinks `[[cible]]`, les hashtags `#tag` et les références de bloc style Obsidian
  `^block-id` sont reconnus quand ils sont présents, ignorés quand ils sont absents.
- Le frontmatter YAML est parsé s'il est présent, sinon inféré (titre depuis le H1 ou le nom de
  fichier, horodatages depuis le stat filesystem).

Quand une note n'a pas d'identifiant stable, Datacron génère un **ULID** et le stocke comme
métadonnée annexe dans `.datacron/`, jamais dans le frontmatter de la note.

---

## 2. Le sidecar `.datacron/`

Un unique dossier caché à la racine du vault contient tout ce dont Datacron a besoin sans
toucher aux notes de l'utilisateur :

```
mon-vault/
├── .datacron/                       # gitignorable, auto-géré
│   ├── VAULT.yaml                   # métadonnées au niveau vault
│   ├── index/                       # index SQLite FTS5 + table annexe des ULID
│   │   └── datacron.db
│   ├── history/                     # octets antérieurs de note, adressés par contenu
│   │   └── <sha256>
│   ├── oplog/                       # preuves d'écriture validées et en attente
│   │   ├── operations.jsonl
│   │   └── pending/
│   │       └── <operation-id>.json
│   ├── scrubber/                    # point de contrôle d'intégrité et sentinelles
│   │   ├── checkpoint.json
│   │   └── canaries/
│   ├── logs/                        # répertoire de logs vault-local réservé
│   └── ulids.json                   # IDs stables pour les notes sans ID de frontmatter
├── ... les notes de l'utilisateur, dans n'importe quelle structure ...
```

Exemple de `.datacron/VAULT.yaml` :

```yaml
datacron_version: "2026.0714.00"   # estampille de provenance : le build Datacron qui a écrit ceci
vault_id: 01HQXR7K9YZ8M2N3PQRSTV4WX5
created: 2026-05-17T14:32:06+02:00
encoding: utf-8
line_endings: lf
# Optionnel : personnalise les noms de dossiers réservés si l'utilisateur a une autre convention
folders:
  drafts: "_drafts"          # défaut
  journal: "_journal"        # défaut
  # L'utilisateur peut mapper vers sa propre structure :
  # drafts: "00-Inbox/AI-Drafts"
```

---

## 3. Le filesystem comme machine à états

Plutôt qu'un champ d'état YAML, Datacron utilise **l'emplacement du dossier** pour suivre le
cycle de vie :

| Dossier (nom par défaut) | Signification | Quand Datacron y écrit |
|---|---|---|
| Partout hors des dossiers réservés | Connaissance canonique approuvée | Jamais (canonique en lecture seule) |
| `_drafts/` | Brouillons générés par l'IA en attente de revue humaine | Quand l'IA crée une nouvelle note (post-v0.2) |
| `_journal/` | Notes datées | Optionnel, structure propre à l'utilisateur |
| `_journal/agent/` | Logs IA en ajout seul (auto-appliqués) | Quand l'IA ajoute une entrée (post-v0.2) |

**Pour promouvoir un brouillon en canonique** : l'utilisateur **déplace simplement le fichier
hors de `_drafts/`**. Aucune édition de frontmatter requise. Le filesystem est la machine à
états.

Les noms de dossiers sont configurables via `.datacron/VAULT.yaml` (voir §2) - l'utilisateur
peut mapper `_drafts/` vers `00-Inbox/AI-Drafts/` si cela colle à sa disposition PARA /
Zettelkasten existante.

---

## 4. Frontmatter des notes mémoire

Quand Datacron crée une note `_memory`, il écrit un frontmatter minimal. Les champs de cycle de
vie bi-temporel sont optionnels et ne sont matérialisés que lorsqu'ils portent une information :

```yaml
---
id: 01HQXR7K9YZ8M2N3PQRSTV4WX5
title: "Synthèse : risques d'adoption de Kafka"
created: 2026-05-17T16:00:00+02:00
updated: 2026-05-17T16:00:00+02:00
origin: ai                      # ai | human | merged
confidence: high
last_verified: 2026-05-17
supersedes: []                  # ULID des notes entièrement remplacées
valid_from: 2026-05-17          # optionnel ; défaut implicite = created
invalid_at: 2026-07-17T08:30:00+00:00  # optionnel ; datetime UTC
invalidated_by: 01HQXR7K9YZ8M2N3PQRSTV4WX6  # optionnel ; ULID remplaçant
tags: [memory/fact]
---
```

`created` est aussi la date d'apprentissage : aucun champ `learned_at` redondant n'est ajouté.
Une note sans `valid_from`, `invalid_at` ni `invalidated_by` reste active et se comporte exactement
comme avant. `invalid_at` conserve le fait dans l'historique mais le démote au même niveau qu'une
note supersédée ; `include_superseded=true` permet de rappeler les deux formes d'historique.

**Les notes existantes ne sont jamais normalisées rétroactivement.** Aucune migration du vault
n'est nécessaire.

---

## 5. Modèle de confiance - 3 états visibles par l'utilisateur

En interne, le moteur de politique supporte un treillis à 6 niveaux (L0-L5), mais l'utilisateur
ne voit que **trois catégories** :

| Catégorie | Niveaux backend | Comportement UX |
|---|---|---|
| **auto-create** | L0, L1 | L'IA crée sans friction (ex. ajout au journal, nouveau brouillon) |
| **review-patch** | L2, L3 | Diff montré à l'utilisateur, une action d'approbation requise |
| **dangerous** | L4, L5 | Double confirmation + tag Git + entrée d'audit |

Une note marquée avec la clé de frontmatter `important: true` est automatiquement escaladée en
catégorie **dangerous** quand l'IA tente de la modifier.

En MVP (v1, lecture seule), tout ce modèle est dormant - Datacron n'écrit rien.

---

## 6. Wikilinks et références

Datacron reconnaît la syntaxe de wikilink compatible Obsidian quand elle est présente :

```markdown
[[note-cible]]
[[note-cible|texte affiché]]
[[note-cible#Titre]]
[[note-cible#^ref-bloc]]
```

Ordre de résolution :
1. Correspondance exacte du `title` de frontmatter
2. Correspondance de nom de fichier (sans `.md`)
3. Correspondance d'`aliases` de frontmatter
4. Correspondance d'ULID (avancé, quand préfixé par `@`)

Les résolutions ambiguës sont **signalées**, jamais résolues silencieusement.

---

## 7. Identifiants de chunk (récupération)

Pour l'indexation de récupération, Datacron utilise des IDs de chunk déterministes :

```
{note_id}::{header_slug_path}::{ordinal}
```

Où :
- `note_id` est l'ULID (généré et stocké dans `.datacron/index/`)
- `header_slug_path` est le slug des titres parents joint par des slashs
- `ordinal` est un index à 4 chiffres complété par des zéros dans la section

Exemple : `01HQXR7K9YZ8M2N3PQRSTV4WX5::architecture/chunking::0003`

---

## 8. Journal d'audit

Les opérations d'écriture validées ajoutent un objet JSON par ligne à
`.datacron/oplog/operations.jsonl`. Les enregistrements en version de format 2 contiennent
`prev_hash`, le SHA-256 de l'enregistrement JSONL canonique précédent (`null` pour le premier),
formant une chaîne infalsifiable. Les journaux existants sans version sont migrés en version 2
en une passe durable avant leur prochain ajout. Les ajouts suivants écrivent et fsync uniquement
le nouvel enregistrement ; `audit_query` effectue une vérification de chaîne complète quand il
lit le journal.

```json
{
  "format_version": 2,
  "operation_id": "01J00000000000000000000042",
  "timestamp": "2026-07-12T12:00:00.000000+00:00",
  "prev_hash": null,
  "op": "patch_section",
  "tool": "patch_note_section",
  "note_id": "01J00000000000000000000043",
  "rel_path": "notes/example.md",
  "before_hash": "<sha256>",
  "after_hash": "<sha256>",
  "actor": "mcp-client",
  "parameters": {"heading": "Example"},
  "history_stored": true
}
```

---

## 9. Engagements de compatibilité

- **Compatibilité ascendante** - Datacron préserve toutes les clés de frontmatter inconnues ; ne supprime jamais de données utilisateur.
- **Compatibilité descendante** - Un vault avec zéro frontmatter Datacron est pleinement supporté.
- **Compatibilité Obsidian** - Tous les formats de vault Obsidian par défaut fonctionnent sans modification (wikilinks, callouts, embeds, tags, blocs).
- **Logseq / Foam / VSCode** - Pareil. Markdown reste Markdown.

---

## 10. Versioning

Deux axes indépendants, volontairement séparés :

- **Ces conventions** (ce document) ont leur propre version - actuellement **v1.1**. Le tableau
  ci-dessous suit les changements des conventions sur disque, pas une version logicielle.
- **`.datacron/VAULT.yaml#datacron_version`** enregistre le *build Datacron* qui a écrit le
  sidecar - une **estampille de provenance** (ex. `2026.0714.00`, la Calendar Version du
  package). Ce **n'est pas** un contrôle de compatibilité de format.

Datacron lit n'importe quel vault Markdown sans contrôle de version ni migration (voir §1). Il
n'existe aujourd'hui aucun gate « refus si majeure supérieure ». Si le format sur disque devait
un jour changer de façon incompatible, un champ de version de format dédié et un contrôle de
compatibilité explicite seraient introduits à ce moment-là - l'estampille de provenance n'est
jamais détournée à cette fin.

| Version des conventions | Date | Changements |
|---|---|---|
| 1.0 | 2026-05-17 | Brouillon initial (déprécié, trop normatif) |
| 1.1 | 2026-05-17 | Bascule vers référence de surcouche, filesystem-comme-machine-à-états, aucune migration requise |

---

## 11. Évolution future

Ce document pourra **éventuellement** être extrait comme standard public si une demande
communautaire émerge (implémentations tierces pour Logseq, Neovim, etc.). Pour l'instant, il
reste une référence interne. Commercialiser la couche de stockage de Datacron comme un standard
ouvert avant qu'elle n'ait fait ses preuves en production serait prématuré.

---

## 12. Licence

Ce document est publié sous la **licence Apache, version 2.0**. Quiconque peut implémenter,
distribuer ou étendre de l'outillage compatible Datacron sans restriction.

L'implémentation de référence est [Datacron](../../README.md) (également Apache 2.0).
