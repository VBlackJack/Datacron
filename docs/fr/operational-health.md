---
title: Santé opérationnelle, mode lecture seule certifié et politique de durabilité
verified: 2026-08-11
tested_on: "Datacron MCP stdio / mcp 2.0.0 / Python 3.11.15"
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
  horodatage de réindex stocké, nombres de notes indexées/vivantes, nombre de chunks,
  cohérence exacte, nombre d'entrées obsolètes, nombre de divergences de hash d'octets et
  secondes d'obsolescence ;
- `integrity` : compteurs en lecture seule vivants pour les incohérences d'ID, les wikilinks
  cassés (`broken_wikilinks`) et leur sous-ensemble bloquant (`broken_wikilinks_misdirected`),
  les notes Markdown à EOL mixtes, les cycles de `supersedes` et les erreurs de parsing ;
- `vault_checksum` : rollup SHA-256 des chemins relatifs triés et des hashes de contenu de note
  exacts aux octets, sur toute note Markdown lisible hors répertoires cachés et de build -
  y compris les dossiers que `excluded_folders` écarte de tout le reste, et portant son propre
  `notes_count` parce que cette portée est plus large que celle sur laquelle `integrity`
  rapporte ;
- `durability` : backend filesystem, support du flush de répertoire, mode sélectionné,
  condition de politique et de durabilité `writes_allowed`, présence d'au moins un chemin
  d'écriture configuré (`write_paths_configured`) et possibilité pour une écriture d'aboutir
  réellement (`effective_writes_enabled`, qui exige `writes_allowed` et au moins un chemin
  configuré) ;
- `scrubber` : dernier scrub terminé, passe et génération d'index courantes, couverture, octets
  vérifiés, état des sentinelles et preuves d'anomalies chemin/type ;
- `invariants` : I1 à I15 depuis le `reliability_evidence.json` packagé.

Le scan est intentionnellement non mis en cache et en O(nombre de notes Markdown). Ne
l'interroge pas comme un endpoint de métriques à haute fréquence.

### Définition de l'obsolescence d'index

Une correspondance exacte indexé-vers-vivant sur le chemin, l'ID et le hash de contenu rapporte
`0.0`. Quand des lignes diffèrent, l'obsolescence est la différence positive entre le mtime le
plus récent d'un fichier vivant et le dernier horodatage d'index stocké. Un horodatage manquant
rapporte `null`. Inspecte toujours `consistent_with_vault` et `stale_entries` ; une ligne
supprimée peut être obsolète même quand la différence d'horodatage est nulle.

`stale_entries` inclut les ajouts de chemin, les suppressions de chemin et les changements de
hash de contenu. `hash_divergences` ne compte que les chemins présents dans les deux vues dont
le hash stocké diffère du SHA-256 exact aux octets courant sur disque. Le `generation` numérique
n'avance qu'après qu'un reconcile a changé l'état complet de l'index ; `generation_hash` reste
le rollup déterministe des lignes chemin, ID et hash de contenu indexées.

La santé reste `degraded` quand l'index est à jour mais que le scan vivant trouve des
incohérences d'ID, des wikilinks mal dirigés, des notes à EOL mixtes, des cycles de
`supersedes` ou des erreurs de parsing de frontmatter. Cela sépare la fraîcheur de l'index du
backlog connu de nettoyage de contenu.

Les wikilinks cassés sont jugés par classification, pas par nombre. Un lien dont la cible
n'existe nulle part (`nonexistent`) est un lien d'intention : certains vaults s'en servent pour
marquer une note qui reste à écrire, donc il compte dans `broken_wikilinks` sans empêcher
`healthy`. Un lien dont la cible existe sous un autre titre ou un autre alias
(`existing_under_other_title_or_alias`) est toujours une erreur : il compte dans
`broken_wikilinks_misdirected` et maintient `degraded`. Sans cette distinction, une convention
d'écriture légitime fige `status` sur `degraded` en permanence, et le seul champ censé alerter
devient un champ qu'on apprend à ignorer.

Une anomalie du scrubber est différente : la santé de haut niveau devient `critical`. Les
alertes du scrubber ne viennent que d'une comparaison directe d'octets du filesystem primaire
ou d'un contrôle de sentinelle configuré. `get_health` ne démarre jamais de scrub et ne répare
aucune anomalie ; il ne fait que lire le point de contrôle durable. Voir
[Scrubber d'intégrité](integrity-scrubber.md) pour le contrat d'exécution, de budget, de reprise
et de sentinelle.

### Ce que le scan regarde

Les compteurs d'`integrity` portent sur les notes que le vault sert réellement : `excluded_folders`
et `excluded_files` de `VAULT.yaml` sont honorés ici exactement comme les honorent le reader,
l'index et la surface MCP. Un défaut situé dans un dossier exclu n'est pas signalé, parce qu'il
n'est pas actionnable - `get_note` refuse un tel chemin avec `note_not_admitted`, et aucun outil
d'écriture ne l'atteint. Le signaler figerait `status` sur `degraded` sans issue, c'est-à-dire
exactement la défaillance que la classification des wikilinks ci-dessus sert à éviter.

`vault_checksum` est l'exception délibérée. Il reste exhaustif et porte son propre `notes_count`,
donc les deux nombres diffèrent sur un vault qui exclut quoi que ce soit. Le restreindre changerait
silencieusement le sens d'une comparaison avec une valeur de référence antérieure, et une
affirmation d'intégrité d'octets dont la portée change en silence vaut moins que pas
d'affirmation du tout.

### Frontière du checksum

Le rollup est un signal ponctuel pour les octets et chemins des notes Markdown. Le comparer à
une valeur antérieure de confiance détecte une altération. Ce n'est pas une preuve de durabilité
future, de comportement du cache matériel, d'intégrité des pièces jointes, ni une protection
contre un attaquant capable de remplacer à la fois les données et la preuve de référence.

## Réparer une incohérence d'identité de note

Une note porte son identité à trois endroits : le champ `id` du frontmatter, le sidecar
`.datacron/ulids.json` et l'index SQLite. `get_health` compte chaque désaccord dans
`integrity.id_mismatches`, et une seule incohérence maintient `status` à `degraded`.

Aucun outil d'écriture MCP ne sait modifier le champ `id`. `set_frontmatter` n'écrit que les
champs de cycle de vie, `patch_note_preamble` édite le corps situé avant le premier titre, et
`datacron ops repair` résout des opérations bloquées, pas des identités. Une seule note divergente
figeait donc `degraded` sans issue sanctionnée. Deux commandes `ops` comblent ce trou.

### `datacron ops inspect-id`

```text
datacron ops inspect-id --vault CHEMIN
```

En lecture seule. Elle liste chaque divergence avec la valeur enregistrée par chacune des trois
sources, la `classification`, le `content_hash` exact à recopier dans la réparation, et l'action
qui la réparerait. Lance-la d'abord : `ops repair-id` refuse de deviner le hash à ta place.

Un `mismatch` est une note dont les sources se contredisent. Un `duplicate` est plusieurs notes
revendiquant le même ID ; il est rapporté et jamais réparé automatiquement, parce que choisir
quelle note garde l'ID est une décision éditoriale.

### `datacron ops repair-id`

```text
datacron ops repair-id --vault CHEMIN --rel-path NOTE.md --action adopt-index --expected-hash HASH --confirm NOTE.md
```

Tous les paramètres sont obligatoires. `--confirm` répète `--rel-path` à l'identique, et
`--expected-hash` est un compare-and-swap strict sur les octets de la note : une note modifiée
depuis l'inspection est refusée, pas écrasée.

`--action adopt-index` est le cas nominal. L'ID canonique - SQLite, ou le sidecar quand l'index
n'en porte aucun - est écrit dans le frontmatter par le chemin d'écriture atomique et journalisé
ordinaire. Le corps et le BOM survivent octet pour octet. Les fins de ligne suivent la
politique d'EOL dominante deja en vigueur dans le vault : une note qui melange CRLF et LF est
normalisee exactement comme n'importe quelle autre ecriture la normalise. Le frontmatter, lui,
est re-serialise dans l'ordre de cles canonique, exactement comme le fait tout autre outil
d'ecriture : un frontmatter ecrit a la main peut donc revenir avec plus de lignes modifiees que le
seul `id` - une liste en style flow est re-emise en style bloc, et un horodatage separe par `T`
revient avec une espace.

`--action adopt-frontmatter` promeut l'ID propre à la note au rang de canonique et réaligne le
sidecar et l'index à la place. Il ne touche pas à la note, et il est refusé quand l'ID du
frontmatter n'est pas un ULID Crockford canonique de 26 caractères. Ce refus est le coeur du
sujet : adopter un ID malformé propagerait exactement le défaut que la commande existe pour
supprimer.

La commande ne génère jamais d'ID neuf et n'en accepte jamais un saisi à la main. Elle échoue
aussi en mode fermé quand la note n'a pas de frontmatter, quand aucune divergence n'est
enregistrée pour ce chemin, quand la divergence est un duplicate, et quand
`.datacron/ulids.json.migrated` associe encore le chemin à un autre ID - ce fichier est fusionné
par-dessus le sidecar primaire par tout lecteur d'identité, donc une entrée périmée y restaurerait
silencieusement la divergence.

Après l'écriture, l'index vivant est réaligné par la même réconciliation incrémentale qu'utilise
la commande `index` : aucun `datacron reindex` hors ligne n'est nécessaire. Cette
réconciliation porte sur tout le vault, pas seulement sur la note réparée : un index qui a
dérivé ailleurs est remis d'aplomb dans la même passe, et les lignes des notes disparues sont
supprimées. La commande rescanne
ensuite le vault et affiche le nombre de divergences qu'elle a résorbées, par exemple
`id_mismatches: 1 -> 0` ; elle sort en code non nul si ce nombre n'a pas baissé.

## Réindex atomique hors ligne

`datacron reindex --vault CHEMIN` construit une base SQLite complète sous un nom temporaire
unique dans le répertoire d'index vivant. Elle lit les notes sans les écrire, stocke des hashes
de contenu exacts aux octets et utilise le parser de wikilinks conscient des fences et de Bash
configuré. Avant publication, elle valide l'égalité exacte de chemin, d'ID et de hash de contenu
contre le vault, vérifie le nombre de notes et la génération suivante, exécute
l'`integrity_check` SQLite et flush la base temporaire.

La commande refuse de démarrer quand un autre processus tient l'index vivant ouvert. Sous Windows,
`os.replace` ne peut pas remplacer un fichier ouvert, et un `datacron mcp serve` en cours le tient
ouvert tant qu'il sert : arrête donc tous les clients et serveurs MCP du vault avant. Le contrôle
coûte un descripteur de fichier ; découvrir la même condition à la publication coûte la
réindexation entière.

La publication utilise un remplacement atomique sur le même filesystem suivi d'un flush de
répertoire. Un échec avant le remplacement préserve l'ancienne génération complète ; un échec
après le remplacement expose la nouvelle génération complète. La commande échoue en mode fermé
si un sidecar `-wal` ou `-shm` vivant existe. Exécute-la comme une opération de maintenance hors
ligne, avec les écrivains de notes au repos et une sauvegarde `.datacron` vérifiée hors du vault.

## Mode lecture seule certifié

Définis :

```text
DATACRON_READ_ONLY=true
```

Le registre MCP vivant omet alors `create_note_ai`, `append_journal`, `set_frontmatter`,
`patch_note_preamble`, `patch_note_section`, `delete_note_section`, `rename_note_section` et
`revert_note`. Les appels directs échouent aussi avec `ReadOnlyModeError`.

La garantie inclut le sidecar `.datacron` : la récupération au démarrage est sautée, l'index
SQLite préconstruit s'ouvre avec `mode=ro`, et la réparation à la lecture de la recherche est
désactivée. Le lecteur vivant conserve le verrouillage et la détection des changements SQLite
afin de suivre sans incohérence les commits d'un autre processus. La sortie du FileLogger est
hors du vault et reste inscriptible. Un index préconstruit est requis ; le mode certifié n'en
crée jamais.

## Mode de durabilité

Définis l'un de :

```text
DATACRON_DURABILITY=best-effort
DATACRON_DURABILITY=strict
```

`best-effort` est le défaut. Si la sonde de flush de répertoire au démarrage n'est pas
supportée, les écritures continuent avec un avertissement FileLogger bruyant et le repli
par-écriture existant.

`strict` refuse toute écriture avec `DurabilityUnavailableError` quand la sonde n'est pas
supportée. Les lectures restent disponibles depuis un index immuable préconstruit.

Sous Windows, la sonde ouvre le répertoire existant avec `FILE_FLAG_BACKUP_SEMANTICS` et appelle
`FlushFileBuffers`. Sous POSIX, elle ouvre le répertoire et appelle `fsync`. La sonde ne crée
aucun fichier. Le succès prouve seulement que la primitive est supportée pour le filesystem, les
permissions et le moment de démarrage courants ; chaque écriture réelle exécute encore son
propre flush de répertoire.
