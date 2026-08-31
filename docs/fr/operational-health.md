---
title: Santé opérationnelle, mode lecture seule certifié et politique de durabilité
verified: 2026-08-30
tested_on: "Datacron 2026.0828.01 / MCP stdio / mcp 2.0.0 / Python 3.11.15"
---

# Santé opérationnelle, mode lecture seule certifié et politique de durabilité

**Français** | [English](../en/operational-health.md)

## `get_health`

`get_health` est un outil MCP en lecture seule destiné à fournir des preuves aux opérateurs et
aux acheteurs. Il ne répare pas l'index, ne récupère pas les opérations en attente, ne purge
pas l'historique et n'écrit pas de résultat en cache.

La réponse contient :

- `status`, `server_version` et le drapeau `read_only` actif ;
- `index` : compteur de génération terminée, hash de génération déterministe, dernier
  horodatage d'indexation stocké par note exposé sous `last_reindex`, nombres de notes
  indexées/vivantes, nombre de chunks, cohérence chemin-et-hash-de-contenu, nombre d'entrées
  obsolètes, nombre de divergences de hash d'octets et secondes d'obsolescence ;
- `integrity` : compteurs en lecture seule vivants pour les incohérences d'ID, les wikilinks
  cassés (`broken_wikilinks`) et leur sous-ensemble bloquant (`broken_wikilinks_misdirected`),
  les notes Markdown à EOL mixtes, les cycles de `supersedes` et les erreurs de lecture, de
  décodage ou de parsing du frontmatter ;
- `vault_checksum` : rollup SHA-256 des chemins relatifs triés et des hashes de contenu de note
  exacts aux octets, sur toute note Markdown lisible hors répertoires cachés et de build -
  y compris les dossiers que `excluded_folders` écarte des lectures admises, de l'indexation et
  des compteurs d'intégrité, et portant son propre `notes_count` parce que cette portée est plus
  large que celle sur laquelle `integrity` rapporte ;
- `durability` : backend filesystem, support du flush de répertoire, mode sélectionné,
  condition de politique et de durabilité `writes_allowed`, présence d'au moins un chemin
  d'écriture configuré (`write_paths_configured`) et leur conjonction
  (`effective_writes_enabled`). Cette conjonction est un prérequis de politique/configuration,
  pas la preuve que les ACL, l'espace libre, l'état de récupération ou une E/S concrète
  autoriseront l'écriture ;
- `recovery` : besoin éventuel de réparer explicitement des opérations bloquées, leur nombre et,
  avec `detail=full`, des preuves bornées sans contenu de note ;
- `scrubber` : dernier scrub terminé, passe et génération d'index courantes, couverture, octets
  vérifiés, état des sentinelles et preuves d'anomalies chemin/type ;
- `invariants` : I1 à I15 depuis le `reliability_evidence.json` packagé.

Le scan est intentionnellement non mis en cache et en O(chemins Markdown + total des octets
Markdown lisibles + lignes d'index). Ne l'interroge pas comme un endpoint de métriques à haute
fréquence.

### Batchs d'organisation bloqués

Avant `apply_organization_manifest`, arrête tous les clients et serveurs Datacron et réalise une
sauvegarde exacte aux octets, vérifiée et hors du vault, des notes affectées et du répertoire
`.datacron` complet. Conserve-la jusqu'à ce que la réponse d'application, le reconcile de l'index,
l'oracle planner et les contrôles de santé soient tous verts.

`datacron ops inspect --vault CHEMIN` rapporte les blocages ordinaires et ceux des batchs
d'organisation. Une raison commençant par `pending_batch_` relève d'une transaction entière : les
deux actions sont indiquées indisponibles, car `ops repair`, limité à une note, ne peut pas résoudre
un membre en sécurité tant que le reste du batch demeure en attente. Arrête tous les writers et ne
supprime ni ne modifie le reçu pending, le stage, l'operation log ou l'historique adressé par
contenu. Préserve une copie forensique, puis restaure la sauvegarde pré-application complète et
vérifiée comme un seul rollback de maintenance hors ligne. Sans cette sauvegarde, arrête-toi et
préserve les preuves pour une récupération manuelle ; ne force ni ne mets en quarantaine un seul
membre. Redémarre Datacron, relance `datacron ops inspect`, puis réconcilie ou réindexe et vérifie
`get_health` avant de reprendre les écritures.

### Définition de l'obsolescence d'index

Une correspondance exacte indexé-vers-vivant sur le chemin et le hash de contenu rapporte `0.0`,
même quand l'index ne contient aucun horodatage. L'ID ne participe pas à ce booléen de cohérence ;
inspecte `integrity.id_mismatches` pour les désaccords d'identité. Quand des lignes diffèrent,
l'obsolescence est la différence positive entre le mtime le plus récent d'un fichier vivant et le
dernier horodatage d'indexation de note stocké. Si cet horodatage est indisponible ou s'il n'existe
aucun mtime de note vivante, elle rapporte `null`. Inspecte toujours `consistent_with_vault` et
`stale_entries` ; une ligne supprimée peut être obsolète même quand la différence d'horodatage est
nulle.

`stale_entries` inclut les ajouts de chemin, les suppressions de chemin et les changements de
hash de contenu. `hash_divergences` ne compte que les chemins présents dans les deux vues dont
le hash stocké diffère du SHA-256 exact aux octets courant sur disque. Le `generation` numérique
avance après qu'un reconcile incrémental a changé l'état complet de l'index, ainsi qu'après chaque
publication réussie d'un reindex complet, y compris vide. `generation_hash` reste le rollup
déterministe des lignes chemin, ID et hash de contenu indexées. Malgré son nom public,
`last_reindex` vaut `MAX(notes.indexed_at)`, pas l'heure de fin d'un reconcile : une passe qui ne
fait que supprimer peut avancer `generation` sans le modifier.

La santé reste `degraded` quand l'index est à jour mais que le scan vivant trouve des
incohérences d'ID, des wikilinks mal dirigés, des notes à EOL mixtes, des cycles de `supersedes`
ou des erreurs de lecture, de décodage ou de parsing du frontmatter. Cela sépare la fraîcheur de
l'index du backlog connu de nettoyage de contenu.

Les wikilinks cassés sont jugés par classification, pas par nombre. Un lien dont la cible
n'existe nulle part (`nonexistent`) est un lien d'intention : certains vaults s'en servent pour
marquer une note qui reste à écrire, donc il compte dans `broken_wikilinks` sans empêcher
`healthy`. Un lien dont la cible existe sous un autre titre ou un autre alias
(`existing_under_other_title_or_alias`) est toujours une erreur : il compte dans
`broken_wikilinks_misdirected` et maintient `degraded`. Sans cette distinction, une convention
d'écriture légitime fige `status` sur `degraded` en permanence, et le seul champ censé alerter
devient un champ qu'on apprend à ignorer.

Une anomalie du scrubber est différente : la santé de haut niveau devient `critical`. Un point de
contrôle lisible peut porter des anomalies issues d'une comparaison directe d'octets du filesystem
primaire ou d'un contrôle de sentinelle configuré. Si le point de contrôle ne peut pas être lu ou
validé, la santé synthétise à la place une anomalie transitoire `checkpoint_unreadable` en mémoire.
`get_health` ne démarre jamais de scrub et ne répare aucune anomalie ; il lit le point de contrôle
durable quand c'est possible et signale autrement cet échec de lecture. Voir
[Scrubber d'intégrité](integrity-scrubber.md) pour le contrat d'exécution, de budget, de reprise
et de sentinelle.

### Ce que le scan regarde

Les compteurs d'`integrity` portent sur les notes admises par le `VAULT.yaml` courant :
`excluded_folders` et `excluded_files` sont rechargés à chaque scan. Le reader et l'index de longue
durée utilisent la politique capturée au démarrage du serveur ; après une modification de ces
réglages, redémarre le serveur et réconcilie l'index avant d'attendre l'accord des trois vues. Un
défaut situé dans un dossier exclu n'est pas signalé, et `get_note` refuse un tel chemin avec
`note_not_admitted`.

L'exclusion est une politique de lecture/admission, pas une ACL d'écriture. Les outils
ordinaires limités à une note autorisent leurs chemins séparément via `DATACRON_WRITE_PATHS` ; un
chemin exclu qui est aussi autorisé en écriture reste donc atteignable par un appel direct à un
mutateur ordinaire. `apply_organization_manifest` est plus strict : chaque source et cible note doit
aussi passer la politique live d'admission et rester dans le `organization.scope` live inchangé.
Garde les chemins d'écriture disjoints du contenu exclu si l'exclusion doit aussi signifier non
inscriptible pour les mutateurs ordinaires.

`vault_checksum` est l'exception délibérée. Il reste exhaustif et porte son propre `notes_count`,
donc les deux nombres diffèrent quand au moins une note Markdown lisible est réellement exclue.
Le restreindre changerait silencieusement le sens d'une comparaison avec une valeur de référence
antérieure, et une affirmation d'intégrité d'octets dont la portée change en silence vaut moins
que pas d'affirmation du tout.

### Frontière du checksum

Le rollup est un signal ponctuel pour les octets et chemins des notes Markdown. Le parcours du
filesystem n'est pas un snapshot atomique : utilise un vault au repos pour obtenir un checkpoint
reproductible, car des modifications concurrentes peuvent mélanger plusieurs instants. Comparer
un résultat stable à une valeur antérieure de confiance détecte une altération. Ce n'est pas une
preuve de durabilité future, de comportement du cache matériel, d'intégrité des pièces jointes, ni
une protection contre un attaquant capable de remplacer à la fois les données et la preuve de
référence.

## Réparer une incohérence d'identité de note

Une note porte son identité à trois endroits : le champ `id` du frontmatter, le sidecar
`.datacron/ulids.json` et l'index SQLite. `get_health` compte chaque désaccord dans
`integrity.id_mismatches`, et une seule incohérence maintient `status` à `degraded`.

`set_frontmatter` n'écrit que les champs de cycle de vie, `patch_note_preamble` édite le corps situé
avant le premier titre, et `datacron ops repair` résout des opérations bloquées, pas des identités.
`revert_note` peut restaurer les octets exacts d'un historique, y compris un ancien `id`, mais
seulement en inversant une opération enregistrée ; il ne peut ni choisir ni canonicaliser une
identité. Une note divergente n'avait donc aucune réparation ciblée sanctionnée. Deux commandes
`ops` comblent ce trou.

### `datacron ops inspect-id`

```text
datacron ops inspect-id --vault CHEMIN
```

En lecture seule. Elle liste chaque divergence avec la valeur enregistrée par chacune des trois
sources, la `classification`, le `content_hash` exact à recopier dans la réparation, et l'action
préférée qui passe ses précontrôles de collision et de sidecar migré, ou la raison pour laquelle
aucune action ne peut être proposée. Lance-la d'abord : `ops repair-id` refuse de deviner le hash
à ta place et répète ses préconditions sur l'état courant ; il peut donc encore refuser après une
inspection si cet état change.

Un `mismatch` est une note dont les sources se contredisent. Un `duplicate` est plusieurs notes
revendiquant le même ID ; il est rapporté et jamais réparé automatiquement, parce que choisir
quelle note garde l'ID est une décision éditoriale.

### `datacron ops repair-id`

```text
datacron ops repair-id --vault CHEMIN --rel-path NOTE.md --action adopt-index --expected-hash HASH --confirm NOTE.md
```

`--rel-path`, `--action`, `--expected-hash` et `--confirm` sont obligatoires. `--vault` est
optionnel et se replie sur `DATACRON_VAULT_ROOT`, ou sur le répertoire courant seulement s'il
contient `.datacron/VAULT.yaml`. `--confirm` répète `--rel-path` à l'identique, et
`--expected-hash` est un compare-and-swap strict sur les octets de la note : une note modifiée
depuis l'inspection est refusée, pas écrasée.

`--action adopt-index` est le cas nominal. Quand le frontmatter diffère, l'ID canonique - SQLite,
ou le sidecar quand l'index n'en porte aucun - est écrit par le chemin de note atomique et
journalisé ordinaire. Quand le frontmatter porte déjà cet ID, la note n'est pas réécrite et seules
les sources sidecar/index divergentes sont réalignées. Pour une note réécrite, le BOM et les octets
du corps sont préservés quand les fins de ligne sont uniformes. Une note qui mélange CRLF et LF est
au contraire normalisée vers son EOL dominant, exactement comme n'importe quelle autre écriture
structurée de note la normalise. Le frontmatter est re-sérialisé dans l'ordre de clés canonique :
un frontmatter écrit à la main peut donc revenir avec plus de lignes modifiées que le seul `id` -
une liste en style flow est réémise en style bloc, et un horodatage séparé par `T` revient avec une
espace.

`--action adopt-frontmatter` promeut l'ID propre à la note au rang de canonique et réaligne le
sidecar et l'index à la place. Il ne touche pas à la note, et il est refusé quand l'ID du
frontmatter n'est pas un ULID Crockford canonique de 26 caractères. Ce refus est le coeur du
sujet : adopter un ID malformé propagerait exactement le défaut que la commande existe pour
supprimer.

La commande ne génère jamais d'ID neuf et n'en accepte jamais un saisi à la main. Elle échoue
aussi en mode fermé quand la note n'a pas de frontmatter, quand aucune divergence n'est
enregistrée pour ce chemin, quand la divergence est un duplicate, et quand
`.datacron/ulids.json.migrated` associe encore le chemin à un autre ID. `JsonIdStore` donne la
priorité au fichier migré, tandis que le scanner de fiabilité et la migration d'index préfèrent le
sidecar primaire. Le refus empêche ces lecteurs de diverger silencieusement après la réparation.

Après l'écriture, l'identité réparée est réalignée par la même réconciliation incrémentale
qu'utilise la commande `index` : aucun `datacron reindex` hors ligne n'est nécessaire pour cette
réparation. La réconciliation parcourt tout le vault et supprime les lignes des notes disparues,
mais son filtre mtime fait confiance aux autres lignes dont le mtime n'a pas changé, sans les relire
ni les hasher. Une dérive étrangère limitée à l'index peut donc persister et exige un reindex hors
ligne. La commande rescanne ensuite le vault et affiche le nombre de divergences qu'elle a
résorbées, par exemple `id_mismatches: 1 -> 0` ; elle sort en code non nul si ce nombre n'a pas
baissé.

## Réindex atomique hors ligne

`datacron reindex --vault CHEMIN` construit une base SQLite complète sous un nom temporaire
unique dans le répertoire d'index vivant. Elle lit les notes sans les écrire, stocke des hashes
de contenu exacts aux octets et utilise le parser de wikilinks conscient des fences et de Bash
configuré. Avant publication, elle valide l'égalité exacte de chemin, d'ID et de hash de contenu
contre le vault, vérifie le nombre de notes et la génération suivante, exécute
l'`integrity_check` SQLite et flush la base temporaire.

Sous Windows, la commande refuse de démarrer quand un autre processus tient l'index vivant ouvert :
`os.replace` ne peut pas remplacer un fichier ouvert, et un `datacron mcp serve` en cours le tient
ouvert tant qu'il sert. POSIX permet de remplacer un fichier ouvert ; aucun précontrôle équivalent
n'y existe et un lecteur déjà ouvert peut rester attaché à l'ancien fichier jusqu'à sa réouverture.
Arrête tous les clients et serveurs MCP du vault avant l'opération sur toutes les plateformes. Le
contrôle Windows coûte un descripteur de fichier ; découvrir la même condition à la publication
coûte la réindexation entière.

La publication utilise un remplacement atomique sur le même filesystem puis tente un flush de
répertoire. Un flush échoué ou indisponible est journalisé comme dégradé ; la nouvelle génération
peut donc être visible sans durabilité confirmée des métadonnées de répertoire. Un échec avant le
remplacement préserve l'ancienne génération complète ; un échec après le remplacement expose la
nouvelle génération complète. La commande échoue en mode fermé si un sidecar `-wal` ou `-shm`
vivant existe. Exécute-la comme une opération de maintenance hors ligne, avec les écrivains de
notes au repos et une sauvegarde `.datacron` vérifiée hors du vault.

## Mesurer l'organisation en intégration continue

`datacron reorganize --dry-run` rapporte l'écart entre le vault et le bloc `organization` de
`VAULT.yaml`. La commande est en lecture seule par construction : elle ne déplace, ne renomme
et ne réécrit jamais une note. `--dry-run` est obligatoire et ne doit jamais devenir implicite.

| Code de sortie | Signification |
|---|---|
| `0` | Aucun écart |
| `1` | Le rapport n'est pas vide |
| `2` | Le vault ou sa configuration n'a pas pu être lu |

`1` n'est pas une erreur. La séparation entre `1` et `2` existe précisément pour qu'un rapport
non vide reste détectable en intégration continue sans faire échouer le job pour une mauvaise
raison : un job distingue une dérive d'organisation d'une configuration cassée par le seul code
de sortie.

```text
datacron reorganize --vault /path/to/vault --dry-run --json
```

`--json` rend un document stable identifié par `organization-plan-v1`, sérialisé de façon
déterministe : deux exécutions sur un vault inchangé produisent le même rapport. Les compteurs
vérifient toujours `scanned = governed + unmatched`, et une note qu'aucune règle ne réclame est
comptée dans `unmatched` sans être un écart.

Schéma complet, gabarits de nom et contrat de rapport :
[Organisation du vault](organization.md).

## Mode lecture seule certifié

Définis :

```text
DATACRON_READ_ONLY=true
```

Le registre MCP vivant omet alors `create_note_ai`, `append_journal`, `set_frontmatter`,
`patch_note_preamble`, `patch_note_section`, `delete_note_section`, `rename_note_section` et
`revert_note`, ainsi que `apply_organization_manifest`. Les appels directs échouent aussi avec
`ReadOnlyModeError`.

La garantie inclut le sidecar `.datacron` : la récupération au démarrage est sautée, l'index
SQLite préconstruit s'ouvre avec `mode=ro`, et la réparation à la lecture de la recherche est
désactivée. Le lecteur vivant conserve le verrouillage et la détection des changements SQLite afin
de suivre sans incohérence les commits d'un autre processus. Le FileLogger reste actif dans
`DATACRON_LOG_DIR` ; sa valeur par défaut est `~/.datacron/logs`, mais le réglage est configurable
et le mode lecture seule certifié ne garantit pas que le répertoire de logs soit hors du vault.
Un index préconstruit est requis ; le mode certifié n'en crée jamais.

## Mode de durabilité

Définis l'un de :

```text
DATACRON_DURABILITY=best-effort
DATACRON_DURABILITY=strict
```

`best-effort` est le défaut. Si la sonde de flush de répertoire au démarrage n'est pas supportée,
les mutations gouvernées par la politique d'écriture continuent avec un avertissement FileLogger
bruyant et le repli par-écriture existant.

`strict` refuse ces mutations gouvernées par la politique avec `DurabilityUnavailableError` quand
la sonde n'est pas supportée. Les commandes de maintenance qui contournent
`WritePolicy.ensure_writable` sont hors de cette gate. Les lectures restent disponibles depuis un
index préconstruit ouvert en lecture seule.

Sous Windows, la sonde ouvre le répertoire existant avec `FILE_FLAG_BACKUP_SEMANTICS` et appelle
`FlushFileBuffers`. Sous POSIX, elle ouvre le répertoire et appelle `fsync`. La sonde ne crée
aucun fichier. Le succès prouve seulement que la primitive est supportée pour le filesystem, les
permissions et le moment de démarrage courants. Les écritures de notes par VaultWriter tentent leur
propre flush de répertoire ; si cette tentative est indisponible ou échoue, elles utilisent un
fallback dégradé de fsync du fichier cible et journalisent un avertissement. Les chemins de
maintenance qui contournent VaultWriter n'héritent même pas de ce comportement ; `ops repair-id`
réaligne actuellement `ulids.json` par remplacement d'un fichier temporaire sans flush explicite
du fichier ni du répertoire.
