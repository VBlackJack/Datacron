# Santé opérationnelle, mode lecture seule certifié et politique de durabilité

**Français** · [English](../en/operational-health.md)

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
  cassés, les notes Markdown à EOL mixtes, les cycles de `supersedes` et les erreurs de
  parsing ;
- `vault_checksum` : rollup SHA-256 des chemins relatifs triés et des hashes de contenu de note
  exacts aux octets ;
- `durability` : backend filesystem, support du flush de répertoire, mode sélectionné, et si
  les écritures sont actuellement autorisées ;
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
incohérences d'ID, des wikilinks cassés, des notes à EOL mixtes, des cycles de `supersedes` ou
des erreurs de parsing de frontmatter. Cela sépare la fraîcheur de l'index du backlog connu de
nettoyage de contenu.

Une anomalie du scrubber est différente : la santé de haut niveau devient `critical`. Les
alertes du scrubber ne viennent que d'une comparaison directe d'octets du filesystem primaire
ou d'un contrôle de sentinelle configuré. `get_health` ne démarre jamais de scrub et ne répare
aucune anomalie ; il ne fait que lire le point de contrôle durable. Voir
[Scrubber d'intégrité](integrity-scrubber.md) pour le contrat d'exécution, de budget, de reprise
et de sentinelle.

### Frontière du checksum

Le rollup est un signal ponctuel pour les octets et chemins des notes Markdown. Le comparer à
une valeur antérieure de confiance détecte une altération. Ce n'est pas une preuve de durabilité
future, de comportement du cache matériel, d'intégrité des pièces jointes, ni une protection
contre un attaquant capable de remplacer à la fois les données et la preuve de référence.

## Réindex atomique hors ligne

`datacron reindex --vault CHEMIN` construit une base SQLite complète sous un nom temporaire
unique dans le répertoire d'index vivant. Elle lit les notes sans les écrire, stocke des hashes
de contenu exacts aux octets et utilise le parser de wikilinks conscient des fences et de Bash
configuré. Avant publication, elle valide l'égalité exacte de chemin, d'ID et de hash de contenu
contre le vault, vérifie le nombre de notes et la génération suivante, exécute
l'`integrity_check` SQLite et flush la base temporaire.

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
`patch_note_section` et `revert_note`. Les appels directs échouent aussi avec
`ReadOnlyModeError`.

La garantie inclut le sidecar `.datacron` : la récupération au démarrage est sautée, l'index
SQLite préconstruit s'ouvre avec `mode=ro&immutable=1`, et la réparation à la lecture de la
recherche est désactivée. La sortie du FileLogger est hors du vault et reste inscriptible. Un
index préconstruit est requis ; le mode certifié n'en crée jamais.

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
