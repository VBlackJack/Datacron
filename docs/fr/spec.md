# Datacron - Contrat public du vault et du serveur MCP

**Français** | [English](../en/spec.md)

> **Statut** : Spec v2.0 - normative, synchronisée avec `main`
> **Auteur** : Julien Bombled
> **Date** : 2026-07-21
> **Remplace** : v1.1 (2026-05-17)
> **Licence** : [Apache 2.0](../../LICENSE)
> **Portée** : Ce document définit les formats, invariants et surfaces observables de
> Datacron. Les choix de conception et les ADR restent dans
> [architecture.md](architecture.md).

Les mots "doit", "ne doit pas" et "jamais" expriment des contrats de l'implémentation
actuelle. Ce document ne décrit aucun comportement futur ou aspirationnel.

---

## 1. Lecture d'un vault et zéro migration

Datacron accepte tout dossier contenant des fichiers Markdown sans imposer de migration, de
structure de dossiers ou de frontmatter. Les notes existantes ne sont pas normalisées ni
réécrites pendant une lecture ou une indexation.

- Le frontmatter YAML est optionnel. S'il est présent et valide, Datacron le parse.
- Le titre vient du champ `title`, puis du premier H1 non vide, puis du nom de fichier.
- Les dates absentes ou invalides viennent des timestamps du filesystem.
- Les hashtags valides viennent du frontmatter et du corps Markdown, hors code inline et blocs
  de code fenced.
- Une note sans identifiant de frontmatter reçoit un ULID déterministe. Il est conservé dans le
  sidecar lorsque celui-ci est inscriptible; en lecture seule, le même ULID est dérivé sans
  écriture. Cet identifiant n'est pas injecté dans la note.
- Un frontmatter YAML invalide n'empêche pas la lecture: le fichier entier est alors traité
  comme corps Markdown avec des métadonnées vides.

Le vault Markdown reste la source de vérité. L'index et les autres données du sidecar sont des
données dérivées ou opérationnelles.

---

## 2. Sidecar `.datacron/` et `VAULT.yaml`

Le sidecar se trouve à la racine du vault. Ses éléments sont créés à l'initialisation ou à la
demande; la présence de chaque sous-dossier n'est donc pas garantie avant la fonction qui
l'utilise.

| Chemin | Contrat observable |
|---|---|
| `.datacron/VAULT.yaml` | Métadonnées du vault et réglages de lecture |
| `.datacron/index/datacron.db` | Index SQLite FTS5, chunks et métadonnées d'index |
| `.datacron/ulids.json` | Identifiants stables des notes sans `id` de frontmatter |
| `.datacron/history/<sha256>` | Octets antérieurs adressés par contenu en mode d'historique `full` |
| `.datacron/oplog/operations.jsonl` | Journal JSONL des écritures validées |
| `.datacron/oplog/pending/` | Manifestes récupérables des écritures en cours |
| `.datacron/locks/` | Verrous consultatifs locaux créés lors des écritures |
| `.datacron/scrubber/` | Checkpoint et canaries du contrôle d'intégrité |
| `.datacron/logs/` | Répertoire vault-local créé par l'initialisation mais non choisi par défaut pour les logs runtime |

Par défaut, les logs runtime sont écrits sous `~/.datacron/logs`; `DATACRON_LOG_DIR` peut
sélectionner un autre emplacement.

`VAULT.yaml` accepte notamment `datacron_version`, `vault_id`, `created`, `encoding`,
`line_endings`, `history_retention_days`, `history_mode`, `folders`, `excluded_folders`,
`excluded_files` et `query_expansion`. `line_endings` vaut `lf` ou `crlf`; `history_mode` vaut
`full` ou `redacted`. `datacron_version` est une estampille de provenance du build qui a écrit
le fichier, pas un gate de compatibilité de format.

Le mapping `folders` est chargé comme métadonnée de configuration. Il ne définit ni une machine
à états, ni les limites d'écriture. Les limites d'écriture viennent exclusivement de
`DATACRON_WRITE_PATHS`.

---

## 3. Frontmatter mémoire et cycle de vie

`create_note_ai` écrit une nouvelle note Markdown avec les champs suivants. Les champs
optionnels restent absents tant qu'aucune valeur ne leur est donnée.

| Champ | Contrat observable |
|---|---|
| `id` | ULID canonique de 26 caractères |
| `title` | Titre non vide fourni à la création |
| `created`, `updated` | Datetimes ISO 8601 générés à la création; `updated` change lors d'une mise à jour de frontmatter |
| `origin` | `ai`, `human` ou `merged` |
| `confidence` | `high`, `medium`, `low` ou `needs_verification` |
| `last_verified` | Valeur fournie ou date UTC du jour |
| `supersedes` | Liste d'identifiants de notes entièrement remplacées, vide par défaut |
| `rejected` | Liste optionnelle de 16 entrées maximum au format `option -- reason` |
| `tags` | Liste de tags fournie à la création |
| `valid_from` | Date ISO optionnelle, validée mais sans effet direct sur le ranking actuel |
| `invalid_at` | Datetime ISO 8601 UTC optionnel; la note devient historique dans le ranking par défaut |
| `invalidated_by` | ULID optionnel validé, conservé comme provenance sans effet direct sur le ranking actuel |

`set_frontmatter` peut modifier `origin`, `confidence`, `last_verified`, `supersedes`,
`rejected`, `valid_from`, `invalid_at` et `invalidated_by`; il met aussi `updated` à jour et
préserve le corps Markdown. Une liste `rejected` vide supprime cette clé. Toute clé de
frontmatter inconnue est préservée lors d'une sérialisation.

Le ranking temporel observable est conservateur:

- une note référencée par `supersedes`, ou portant `invalid_at`, est démotée par défaut;
- `include_superseded=true` désactive cette démotion historique;
- `confidence=low` et `confidence=needs_verification` appliquent une pénalité de score;
- `valid_from` et `invalidated_by` sont validés et conservés, mais ne changent pas seuls
  l'éligibilité ou le score.

`important: true` n'escalade aucune politique d'écriture. Son seul effet observable actuel est
un marqueur `*` dans la ressource `datacron://vault/map`.

---

## 4. Wikilinks, tags et résolution

Datacron reconnaît les formes de wikilinks suivantes:

```markdown
[[cible]]
[[cible|alias affiché]]
[[cible#Titre]]
[[cible#^ref-bloc]]
```

La cible, l'alias, le heading et la référence de bloc sont extraits du wikilink. Un identifiant
de bloc autonome `^block-id` dans le corps n'est pas indexé comme référence sémantique.

La résolution d'un alias suit trois niveaux globaux, dans cet ordre:

1. correspondance exacte du `title`;
2. correspondance du nom de fichier sans `.md`;
3. correspondance d'un élément de `aliases`.

Une ambiguïté au sein du niveau prioritaire produit une cible non résolue et un log; Datacron ne
choisit pas silencieusement une note. `get_backlinks` accepte aussi un ULID canonique nu comme
cible directe. Le préfixe `@` ne fait pas partie du contrat de résolution.

---

## 5. Identifiants de chunk

Les chunks indexés utilisent un identifiant déterministe:

```text
{note_id}::{header_slug_path}::{ordinal:04d}
```

`note_id` est l'ULID stable de la note, `header_slug_path` est le chemin de headings slugifié et
`ordinal` est un entier sur quatre chiffres. Exemple:

```text
01HQXR7K9YZ8M2N3PQRSTV4WX5::architecture/chunking::0003
```

La stabilité de l'identifiant dépend du contenu et de la structure indexés. Le contrat de
fraîcheur des chunks est défini à la section 15.

---

## 6. Sémantique des chemins

Les dossiers du vault n'encodent aucun état métier imposé. Datacron n'interprète pas
`_drafts`, `_journal` ou un autre dossier comme canonique, approuvé ou dangereux.

Le setup propose `_memory`, `_drafts` et `_journal` comme allowlist d'écriture courante lorsque
l'utilisateur active les write tools. Cette liste est un défaut du setup, pas une règle du
format: une configuration explicite peut autoriser d'autres sous-dossiers du vault.

Tous les chemins lus ou écrits sont résolus et confinés au vault. Les symlinks ou traversées qui
sortent des racines autorisées sont refusés.

---

## 7. Historique des opérations et journal d'audit

Chaque écriture validée ajoute un objet JSON ASCII par ligne à
`.datacron/oplog/operations.jsonl`. Les enregistrements de format 2 contiennent `prev_hash`, le
SHA-256 de la ligne JSON canonique précédente, ou `null` pour la première ligne. La lecture par
`audit_query` vérifie la chaîne complète. Un journal legacy est migré durablement vers le format
2 avant son prochain append.

`operation_id` est un UUID v4 rendu sous forme de 32 caractères hexadécimaux. `note_id` reste un
ULID. Un enregistrement contient également le timestamp UTC, l'opération, le tool, le chemin, les
hashes avant et après, l'acteur, les paramètres expurgés et l'indication `history_stored`.

L'append du journal est flushé et fsync. Les manifestes `pending` permettent de terminer ou de
réconcilier une opération interrompue sans dupliquer un enregistrement déjà validé.

---

## 8. Compatibilité et version du format

- Un vault sans frontmatter Datacron reste lisible et indexable.
- Les clés de frontmatter inconnues sont préservées lors des écritures supportées.
- Le Markdown brut, les callouts, embeds et autres syntaxes non interprétées sont conservés
  comme contenu. La sémantique indexée spécifique est limitée aux headings, tags et formes de
  wikilinks décrites dans cette spec; les identifiants de bloc autonomes ne sont pas résolus.
- `datacron_version` enregistre le build écrivain et ne bloque jamais la lecture d'un vault.
- La version de cette spec est indépendante de la CalVer du package.

| Version de la spec | Date | Changement |
|---|---|---|
| 1.1 | 2026-05-17 | Ancienne référence de surcouche |
| 2.0 | 2026-07-21 | Contrats observables alignés sur l'implémentation de `main` |

---

## 9. Surface des 14 tools MCP

En mode standard, le serveur enregistre 14 tools. En mode certifié read-only
(`DATACRON_READ_ONLY=true`), les cinq tools mutateurs sont retirés et neuf tools restent
exposés.

| Catégorie | Tool | Contrat observable |
|---|---|---|
| Lecture | `list_notes` | Liste paginée, filtrable par dossier, tags et frontmatter de premier niveau |
| Lecture | `get_note` | Lecture par ULID, chunk ID ou chemin, en format `full`, `chunk` ou `map` |
| Lecture | `search_text` | Recherche BM25 FTS5 avec ranking temporel optionnellement historique |
| Lecture | `search_regex` | Recherche regex via ripgrep, avec fallback indexé borné, filtrable par glob |
| Lecture | `get_backlinks` | Chunks dont les wikilinks ciblent un ULID ou un alias résolu |
| Advisory | `contradiction_scan` | Candidats déterministes et proposition de call d'écriture; n'écrit jamais |
| Opérationnel | `get_health` | Fraîcheur, intégrité, checksum, durabilité et preuves d'invariants |
| Écriture | `create_note_ai` | Crée une note mémoire sans overwrite |
| Écriture | `append_journal` | Ajoute une entrée sous un heading d'une note existante |
| Écriture | `set_frontmatter` | Modifie uniquement les champs de cycle de vie autorisés et `updated` |
| Écriture | `patch_note_section` | Remplace le contenu sous un heading existant en conservant la ligne du heading |
| Écriture | `revert_note` | Restaure les octets exacts d'une version d'historique adressée par hash |
| Opérationnel | `get_note_history` | Liste les métadonnées d'opérations validées d'une note sans lire les anciens octets |
| Opérationnel | `audit_query` | Filtre le journal validé par période, tool ou note sans le modifier |

Les corps de note et snippets rendus au client sont confinés, expurgés selon la politique de
secrets et encapsulés comme contenu non fiable. Les titres, chemins, tags et autres métadonnées
de récupération sont assainis et expurgés selon la même politique.

---

## 10. Write tools, allowlist, CAS et historique

Les cinq write tools sont opt-in au niveau des effets:

- `DATACRON_READ_ONLY=true` les retire de la surface MCP;
- sinon ils sont enregistrés, mais une allowlist `DATACRON_WRITE_PATHS` vide rend toute cible
  non autorisée et `policy/active` annonce les écritures comme désactivées;
- chaque cible doit être dans le vault et sous au moins une racine de l'allowlist;
- le mode de durabilité doit autoriser l'écriture.

Chaque write tool accepte un `expected_hash` optionnel. Lorsqu'il est fourni, l'écriture échoue
si le SHA-256 des octets courants diffère: c'est le compare-and-swap (CAS). Une création refuse
toujours d'écraser un fichier existant.

Lorsqu'une mutation cible une note existante, elle stocke les octets antérieurs par SHA-256 en
mode `history_mode=full`. Toute mutation validée écrit un manifeste pending, remplace ou crée
atomiquement la note, ajoute le journal chaîné, puis retire le manifeste. Le mode `redacted`
conserve les hashes et le journal mais pas les anciens octets; `revert_note` ne peut alors pas
relire une version historique. La rétention vaut 30 jours par défaut et est configurable par
`history_retention_days`.

Après une écriture MCP réussie, Datacron réconcilie l'index de façon synchrone. La réponse porte
`indexed: true` seulement après cette réconciliation.

---

## 11. Read allowlist

`DATACRON_READ_PATHS` est une liste de racines absolues après expansion et résolution. Dans une
variable d'environnement, les éléments sont séparés par le séparateur de chemins de l'OS.

- Si la liste est vide, `DATACRON_VAULT_ROOT` est la limite implicite de lecture.
- Si la liste n'est pas vide, le serveur refuse de démarrer si le vault servi n'est contenu dans
  aucune racine autorisée.
- Après le démarrage, chaque lecture reste confinée au vault servi; une traversal ou un symlink
  sortant est refusé.

L'allowlist de lecture n'accorde aucune permission d'écriture.

---

## 12. Clients MCP supportés

Le setup connaît exactement neuf identifiants de client. Le scope `user` est disponible pour
les neuf; le scope `project` n'est écrit que lorsqu'un chemin projet est défini pour le client.

| ID CLI | Nom affiché | Scopes de configuration | Format |
|---|---|---|---|
| `claude-desktop` | Claude Desktop | `user` | JSON `mcpServers` |
| `claude-code` | Claude Code | `user`, `project` | JSON `mcpServers` |
| `cursor` | Cursor | `user`, `project` | JSON `mcpServers` |
| `gemini-cli` | Gemini CLI | `user`, `project` | JSON `mcpServers` |
| `antigravity` | Antigravity | `user`, `project` | JSON `mcpServers` |
| `lmstudio` | LM Studio | `user` | JSON `mcpServers` |
| `codex-cli` | Codex CLI | `user`, `project` | TOML |
| `windsurf` | Windsurf | `user` | JSON `mcpServers` |
| `vscode` | VS Code | `user`, `project` | JSON `servers` |

La découverte est best-effort. Pour Antigravity, elle exige le dossier de profil live
`~/.gemini/antigravity`; les dossiers `antigravity-ide` et `antigravity-backup` ne comptent pas.
Le scope projet Antigravity cible `<project>/.agents/mcp_config.json`; le scope utilisateur cible
`~/.gemini/config/mcp_config.json`. Un fichier JSON vide est traité comme une configuration
absente. L'installation et la désinstallation ne modifient que l'entrée `datacron` et préservent
les autres serveurs.

La découverte de LM Studio exige le vrai dossier de profil `~/.lmstudio`. Sa seule cible est
la configuration utilisateur `~/.lmstudio/mcp.json`; aucune cible projet n'est définie. LM Studio
est exclu de `datacron protocol install`, car aucun fichier d'instructions globales n'est documenté.

---

## 13. Transport stdio

Le transport serveur exposé est MCP sur stdio. `datacron mcp serve` et le point d'entrée
`datacron-mcp` lancent la boucle FastMCP stdio et s'arrêtent lorsque le client se déconnecte ou
que le processus est interrompu.

Le serveur n'ouvre aucun listener réseau et la CLI actuelle n'expose pas de transport HTTP.
Une installation Python configure l'exécutable `datacron-mcp` sans argument; un binaire frozen
configure son propre exécutable avec les arguments `mcp`, `serve`. Les deux formes transmettent
les variables d'environnement du vault.

---

## 14. Durabilité `strict` et `best-effort`

`DATACRON_DURABILITY` accepte exactement `strict` ou `best-effort`; le défaut est
`best-effort`. La capacité de flush d'entrée de répertoire est sondée sur le backend du vault.

| Mode | Si le flush de répertoire est supporté | S'il n'est pas supporté |
|---|---|---|
| `best-effort` | L'écriture atomique est autorisée | L'écriture est autorisée avec un warning explicite |
| `strict` | L'écriture atomique est autorisée | Toute écriture est refusée |

Le mode certifié read-only refuse toujours les écritures, indépendamment du mode de durabilité.
`get_health` et `datacron://policy/active` exposent l'état effectif pertinent.

---

## 15. Contrat de fraîcheur

Le contrat de fraîcheur observable est `freshness-contract-v1`; son détail de calcul est défini
dans [freshness-contract-v1.md](freshness-contract-v1.md).

- Une écriture effectuée par un write tool réconcilie l'index avant de retourner
  `indexed: true`.
- Avant une lecture basée sur l'index, Datacron tente une réparation incrémentale sérialisée
  avec gate `mtime` et autorité `content_hash`. Les sweeps sont espacés de 30 secondes par
  défaut. Une politique qui interdit les mutations d'index n'effectue pas cette réparation.
- Entre deux sweeps, une lecture peut servir l'index courant; `get_health` fournit l'état exact
  pour diagnostiquer un écart après une modification hors Datacron.
- Un `chunk_id` conservé par un client devient périmé si l'identité ou le `content_hash` de sa
  note parent ne correspond plus à l'index. `get_note(chunk_id)` renvoie alors une erreur
  explicite demandant de réindexer et de réessayer; il ne sert jamais silencieusement un chunk
  périmé.

Une modification massive hors ligne doit être suivie de `datacron index`. `datacron reindex`
reconstruit l'index quand une réparation complète est nécessaire.

---

## 16. Resources MCP

Le serveur enregistre exactement trois resources pull-only:

| URI | Type | Contrat observable |
|---|---|---|
| `datacron://vault/map` | `text/markdown` | Arborescence légère avec titres et tags, tronquée au budget; `important: true` ajoute `*` |
| `datacron://vault/info` | `application/json` | Racine, initialisation, compte de notes, chemin et statistiques d'index, limites de résultat |
| `datacron://policy/active` | `application/json` | Mode `read-only` ou `read-write`, activation effective des write tools et allowlist d'écriture |

`policy/active` retourne des listes vides pour `auto-create`, `review-patch`, `dangerous` et
`active_policies`. Le moteur de confiance L0-L5 n'est pas exposé par le serveur actuel.

---

## 17. Frontière documentaire et licence

Cette spec est la référence des contrats observables. La topologie interne, les choix de
composants, les ADR, la sécurité de conception et les limites architecturales sont documentés
dans [architecture.md](architecture.md); ils ne sont pas dupliqués ici.

Cette spec et l'implémentation de référence [Datacron](../../README.md) sont publiées sous la
[licence Apache, version 2.0](../../LICENSE).
