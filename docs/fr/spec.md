---
title: Datacron - Contrat public du vault et du serveur MCP
verified: 2026-08-30
tested_on: "Datacron MCP stdio / mcp 2.0.0 / Python 3.11.15"
---

# Datacron - Contrat public du vault et du serveur MCP

**Français** | [English](../en/spec.md)

> **Statut** : Spec v2.0 - normative pour l'implémentation livrée par cette version
> **Auteur** : Julien Bombled
> **Date** : 2026-08-30
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
- Un BOM UTF-8 initial ne masque pas un frontmatter YAML par ailleurs valide. Il reste dans les
  octets bruts utilisés pour le hash de fraîcheur et, en l'absence de frontmatter, dans le corps
  Markdown.
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
| `.datacron/oplog/batches/` | Payloads préparés et reçus `pending`/`committed` des batches d'organisation récupérables |
| `.datacron/locks/` | Verrous consultatifs locaux créés lors des écritures |
| `.datacron/scrubber/` | Checkpoint et canaries du contrôle d'intégrité |
| `.datacron/logs/` | Répertoire vault-local créé par l'initialisation mais non choisi par défaut pour les logs runtime |

Par défaut, les logs runtime sont écrits sous `~/.datacron/logs`; `DATACRON_LOG_DIR` peut
sélectionner un autre emplacement.

`VAULT.yaml` accepte notamment `datacron_version`, `vault_id`, `created`, `encoding`,
`line_endings`, `history_retention_days`, `history_mode`, `folders`, `excluded_folders`,
`excluded_files`, `query_expansion` et `organization`. `line_endings` vaut `lf` ou `crlf`; `history_mode` vaut
`full` ou `redacted`. `datacron_version` est une estampille de provenance du build qui a écrit
le fichier, pas un gate de compatibilité de format.

Le mapping `folders` est chargé comme métadonnée de configuration. Il ne définit ni une machine
à états, ni les limites d'écriture. Les limites d'écriture viennent exclusivement de
`DATACRON_WRITE_PATHS`.

Le bloc `organization` est facultatif; un vault qui ne le déclare pas n'est pas affecté. Il
porte une portée et des règles associant un tag de frontmatter à un dossier, avec un gabarit
de nom et un plafond de taille optionnels. Il ne modifie ni les limites d'écriture, ni
l'admission des notes : il sert uniquement à mesurer l'écart entre le vault et l'organisation
qu'il déclare. Schéma complet, gabarits de nom et contrat de rapport :
[Organisation du vault](organization.md).

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

Toute création par `create_note_ai` ou `create_exact` exige un ULID Crockford canonique. Pour
remplacer ou déplacer une note existante, le manifeste accepte aussi son identifiant historique
borné à 26 caractères alphanumériques majuscules, mais impose de le préserver strictement dans le
résultat : ce canal ne migre jamais une identité.

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

Un batch d'organisation prépare ses payloads et historiques avant de publier son reçu `pending`.
La reprise n'invente aucun contenu : elle ne poursuit en roll-forward que si chaque chemin porte
encore exactement son hash avant ou après attendu. Tout troisième hash bloque tous les writers et
apparaît dans les preuves de récupération jusqu'à une intervention explicite. Les suppressions de
sources d'un déplacement sont effectuées après les créations et remplacements de cibles. Le reçu
`committed` conserve le hash du manifeste, le jeton de confirmation, le hash du rapport projeté et
les hashes bornés de chaque membre ; il ne conserve aucun payload de note.

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

## 9. Surface des tools MCP

En mode standard, le serveur enregistre exactement le manifeste fermé décrit ci-dessous. En
mode certifié read-only (`DATACRON_READ_ONLY=true`), tous les tools mutateurs sont retirés et
seuls les tools de lecture, advisory et opérationnels restent exposés.

| Catégorie | Tool | Contrat observable |
|---|---|---|
| Lecture | `session_context` | Contexte initial borne et protocole commun versionne. |
| Lecture | `prepare_follow_up` | Prepare les suivis sources sans ecrire. |
| Lecture | `get_follow_up` | Dernieres revisions des suivis structures. |
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
| Écriture | `patch_note_preamble` | Remplace ou supprime le préambule avant le premier titre Markdown reconnu |
| Écriture | `patch_note_section` | Remplace le contenu sous un heading existant en conservant la ligne du heading |
| Écriture | `delete_note_section` | Supprime explicitement une section H2-H6 et son sous-arbre |
| Écriture | `rename_note_section` | Renomme un titre H2-H6 sans modifier son contenu |
| Écriture | `revert_note` | Restaure les octets exacts d'une version d'historique adressée par hash |
| Écriture | `apply_organization_manifest` | Valide puis applique, après confirmation exacte, un bundle d'organisation adressé par contenu |
| Opérationnel | `get_note_history` | Liste les métadonnées d'opérations validées d'une note sans lire les anciens octets |
| Opérationnel | `audit_query` | Filtre le journal validé par période, tool ou note sans le modifier |

Les corps de note et snippets rendus au client sont confinés, expurgés selon la politique de
secrets et encapsulés comme contenu non fiable. Les titres, chemins, tags et autres métadonnées
de récupération sont assainis et expurgés selon la même politique.

---

## 10. Write tools, allowlist, CAS et historique

Les write tools sont opt-in au niveau des effets:

- `DATACRON_READ_ONLY=true` les retire de la surface MCP;
- sinon ils sont enregistrés, mais une allowlist `DATACRON_WRITE_PATHS` vide rend toute cible
  non autorisée et `policy/active` annonce les écritures comme désactivées;
- chaque cible doit être dans le vault et sous au moins une racine de l'allowlist;
- le mode de durabilité doit autoriser l'écriture.

Deux exceptions de chemin sont internes à `apply_organization_manifest`. Chaque source et cible
note doit rester dans l'intersection du `organization.scope` live inchangé, de la politique live
d'admission des notes et de `DATACRON_WRITE_PATHS`. `.datacron/VAULT.yaml` peut être remplacé hors
de cette allowlist sous CAS exact, uniquement si le mapping top-level `organization` est la seule
différence sémantique ; `organization.scope` lui-même reste inchangé en v1. Datacron peut aussi
dériver un membre `.datacron/ulids.json` sous CAS exact, uniquement pour déplacer la clé sidecar
d'une source vers la cible du même `move_replace_exact` ; le manifeste ne fournit jamais
arbitrairement ce payload.

Chaque mutation de note prend en charge le compare-and-swap (CAS) par `expected_hash`.
`patch_note_preamble` exige toujours ce hash ; les autres tools le rendent optionnel sauf quand
leur sélecteur d'occurrence l'exige. Lorsque le hash est fourni, l'écriture échoue si le SHA-256
des octets courants diffère. Une création refuse toujours d'écraser un fichier existant.

Lorsqu'une mutation cible une note existante, elle stocke les octets antérieurs par SHA-256 en
mode `history_mode=full`. Toute mutation validée écrit un manifeste pending, remplace ou crée
atomiquement la note, ajoute le journal chaîné, puis retire le manifeste. Le mode `redacted`
conserve les hashes et le journal mais pas les anciens octets; `revert_note` ne peut alors pas
relire une version historique. La rétention vaut 30 jours par défaut et est configurable par
`history_retention_days`.

Après une écriture MCP réussie, Datacron réconcilie l'index de façon synchrone. La réponse porte
`indexed: true` seulement après cette réconciliation.

`apply_organization_manifest` est le seul mutateur multi-fichiers public. Son `manifest_path`
absolu désigne un bundle local hors du vault : `manifest.json` référence uniquement des payloads
frères sous `payloads/` par leur SHA-256. Le manifeste déclare au moins une opération exacte sur une
note et/ou un remplacement exact de la configuration `organization` ; aucune catégorie n'est
obligatoire lorsque l'autre est présente. Le mode `validate` est sans effet et vérifie strictement
les tailles, hashes, chemins, CAS, identités/alias, scope d'organisation, seul changement permis
dans `VAULT.yaml` et rapport planner projeté. Il retourne un jeton qui signe l'ensemble de ces
préconditions sans exposer le contenu. Le digest de scope couvre toutes les notes Markdown admises
dans `organization.scope` et le pré-état exact des sidecars d'identité ; le pré-hash exact de la
configuration est lié séparément. Les octets de notes sans rapport situées hors de ce scope ne font
pas partie du token. Le mode `apply` exige ce jeton exact, recharge et revalide le bundle sous le
verrou global de mutation, puis accepte seulement `create_exact`, `replace_exact`,
`move_replace_exact` et le remplacement CAS exact de `.datacron/VAULT.yaml` où seul le mapping
top-level `organization` peut changer sémantiquement et où `organization.scope` reste inchangé.
Une source existante doit porter l'`id` attendu dans son frontmatter ; une note identifiée
uniquement par sidecar est hors du schéma v1. Si un déplacement possède aussi une entrée redondante
dans `ulids.json`, Datacron dérive sa migration comme membre interne. Il peut aussi supprimer une
collision de casse obsolète seulement si l'inventaire live prouve mécaniquement la clé exacte et
l'ID non réutilisé. Le mode validate expose le nombre de ces canonicalisations et leur SHA-256
content-free ; le token, le journal et le reçu durable lient les preuves exactes utilisées par la
recovery et la purge d'index.

Le mode `validate` refuse déjà le bundle si `history_mode` n'est pas `full`; aucun jeton exécutable
n'est émis quand les octets antérieurs ne pourraient pas être conservés.

Le batch est récupérable après incident et chaque remplacement de fichier est atomique. Il ne
promet pas que plusieurs chemins deviennent visibles au même instant. Tous les autres clients et
serveurs Datacron doivent donc être arrêtés pendant cette fenêtre de maintenance. Une réussite
fraîche effectue exactement une réconciliation d'index et invalide le cache d'alias ; le hash du
rapport planner final doit égaler le hash projeté. Un réappel du même manifeste validé rend le reçu
durable sans réécrire les notes. `operation_count` compte les opérations déclarées et le membre
config ; `derived_operation_count` et `identity_sidecar_replaced` rendent explicite tout membre
interne ; `identity_sidecar_case_canonicalization_count` et son SHA-256 bornent son nettoyage de
casse.
`total_payload_bytes` décrit les payloads externes du bundle. Après commit durable, une panne de
réconciliation ou un oracle planner différent retourne respectivement
`committed_index_incomplete` ou `committed_report_mismatch`, avec `already_committed: true|false`
selon qu'il s'agit d'un replay ; ce n'est jamais présenté comme une absence de mutation.

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
`datacron-mcp` lancent la boucle `MCPServer` du SDK Python MCP v2 (`mcp>=2,<3`) et s'arrêtent
lorsque le client se déconnecte ou que le processus est interrompu.

Le serveur n'ouvre aucun listener réseau et la CLI actuelle n'expose pas de transport HTTP.
Une installation Python configure l'exécutable `datacron-mcp` sans argument; un binaire frozen
configure son propre exécutable avec les arguments `mcp`, `serve`. Les deux formes transmettent
les variables d'environnement du vault.

Le même serveur stdio prend en charge le protocole moderne final `2026-07-28` et le mode legacy
`2025-11-25`, sans flag installateur distinct. Les tests stdio réels couvrent les deux modes.
Datacron n'active aucun transport HTTP du SDK.

Un outil inconnu et une ressource absente renvoient l'erreur JSON-RPC `-32602`. Le serveur
préserve ce contrat pour les outils par une traduction après le lookup public
`MCPServer.call_tool`, sans gestionnaire privé. Une panne interne de ressource est assainie en
`-32603`. Les erreurs de validation d'un outil connu restent un résultat d'outil avec le champ
wire `isError=true`. Seules les exceptions métier Datacron transformées par le wrapper
conservent le payload texte stable `{"error": {"type": ..., "message": ...}}`. Les erreurs
SDK/protocole restent distinctes.

L'elicitation push via `ctx.elicit()` exige une session legacy `2025-11-25` dotée d'un
back-channel. Sur `2026-07-28`, `contradiction_scan` ne tente pas ce push : il retourne son scan
normal et laisse toute confirmation à un appel explicite ultérieur.

Références officielles : [publication finale MCP 2026-07-28](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
et [nouveautés du SDK Python MCP v2](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md).

---

## 14. Durabilité `strict` et `best-effort`

`DATACRON_DURABILITY` accepte exactement `strict` ou `best-effort`; le défaut est
`best-effort`. La capacité de flush d'entrée de répertoire est sondée sur le backend du vault.

| Mode | Si le flush de répertoire est supporté | S'il n'est pas supporté |
|---|---|---|
| `best-effort` | La mutation atomique gouvernée par la politique est autorisée | La mutation gouvernée par la politique est autorisée avec un warning explicite |
| `strict` | La mutation atomique gouvernée par la politique est autorisée | La mutation gouvernée par la politique est refusée avec `DurabilityUnavailableError` |

Ces lignes couvrent les mutations passant par `WritePolicy.ensure_writable` ; les chemins de
maintenance locale qui contournent cette politique sont hors de cette gate de durabilité. Le mode
certifié read-only refuse toujours les mutations MCP enregistrées, indépendamment du mode de
durabilité, mais les commandes de maintenance locale restent une surface opérateur distincte.
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


## Garanties de lecture et erreurs après écriture

Le masquage examine le texte source avant surlignage. Si un secret est détecté, la réponse
utilise l'extrait source masqué sans surlignage ; les autres extraits restent surlignés.
Les lignes des chunks correspondent au fichier physique, avec frontmatter, LF/CRLF et BOM UTF-8.
Une lecture par ULID vérifie l'identité du fichier courant plutôt que de croire un ancien chemin.

Le masquage des chunks et recherches examine aussi les zones sensibles de la note parente
complete. Un fragment qui ne contient qu'une partie du secret est masque par `[REDACTED]` ;
les chunks publics independants restent lisibles. Les octets et hashes restent inchanges.
Chaque fragment indexe est compare aux chunks recalcules sur le parent courant avant masquage.
Les fragments inchanges restent utilisables en lecture seule ; un fragment non verifiable est refuse.

`DATACRON_MAX_RESULT_TOKENS` borne le tableau de resultats serialise selon l'estimation de quatre
caracteres par token, echappement JSON, metadonnees et enveloppes compris. Les champs externes
du tool et la requete repetee sont separes. `token_count` decrit toujours le chunk indexe, pas
l'extrait rendu. Une coupe ou une omission active `truncated_for_tokens=true`. L'extrait regex
conserve la zone correspondante si elle tient ; si les metadonnees seules depassent le budget,
moins de resultats sont retournes.

La limite regex compte les correspondances resolues et admises, pas les occurrences brutes du
frontmatter ou des fichiers. Les trames ripgrep sont lues progressivement et bornees chacune
par `DATACRON_REGEX_MAX_FRAME_BYTES` (8 Mio par defaut). Un depassement termine le processus enfant
et retourne `error.code="regex_frame_too_large"`. Reduire le glob ou augmenter deliberement
cette limite pour de grandes entrees de confiance.

Si une écriture ordinaire réussit mais que la réconciliation échoue, le résultat MCP est une
erreur portant `error.code="committed_index_incomplete"`, `error.committed=true`,
`error.indexed=false`, `error.content_hash` (octets validés) et `error.correlation_id` pour le diagnostic.
**Ne répète pas la mutation.** Relis la note, diagnostique/répare l'index et vérifie sa santé.
Le hash décrit cette écriture, sans garantir qu'aucun autre écrivain n'a modifié le fichier depuis.
Sans `request_id`, une relance avec l'ancien hash est refusée par CAS ; sans hash, un ajout
peut être dupliqué. Les erreurs avant commit confirmé conservent leur contrat existant.
Sans `request_id`, une annulation ou une réponse perdue impose de vérifier la note et son
historique. Avec un `request_id` stable, rejouer exactement les mêmes arguments permet
de retrouver le reçu historique sans répéter une modification déjà effectuée.


Voir [Améliorations de fiabilité](improvements.md) pour le rejeu des écritures, l’indexation ciblée, la sélection Markdown commune et les contrôles qualité.
