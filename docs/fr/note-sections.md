# Lire et réorganiser les sections d'une note

[English version](../en/note-sections.md)

Utilisez le plan d'une note pour sélectionner la partie utile. Les opérations de
section utilisent le même analyseur Markdown que le plan : le balisage en ligne est
retiré des noms de titres, les titres Setext sont reconnus et les titres placés dans
des blocs de code sont ignorés.

## Lire une section

Appelez `get_note` avec `format: "map"` pour consulter la hiérarchie. Passez ensuite
les noms de titres rendus, de la racine à la cible, dans `heading_path` :

```json
{
  "id_or_path": "backlog.md",
  "heading_path": ["Backlog", "Actifs", "Service", "BL-0123"],
  "format": "full"
}
```

Le résultat contient le titre choisi et tout son sous-arbre, dans la limite habituelle
de réponse. `offset`, `limit`, `total_chars` et `next_offset` portent sur le texte
expurgé de la section. Conservez le même chemin de titres pour lire la page suivante.
Les métadonnées de section indiquent les lignes sources et l'occurrence sélectionnée ;
`content_hash` et `note_content_hash` décrivent toujours la note originale complète.

Si le même chemin complet apparaît plusieurs fois, précisez `heading_occurrence`,
à partir de un. Un sélecteur absent ou ambigu produit une erreur. La sélection exige
un chemin ou un identifiant de note et `format: "full"` ; elle ne se combine pas avec
un identifiant de fragment. Un fragment inconnu produit également une erreur au lieu
de renvoyer toute la note. Relisez le plan et utilisez un identifiant réellement
retourné par le serveur.

## Déplacer une section dans une note

`move_note_section` déplace un titre et son sous-arbre sous un titre de destination
existant dans la même note. Cela permet d'archiver un item terminé en conservant son
identifiant et son historique. L'outil ne décide pas si l'item est terminé : vérifiez
son dernier état avant de le sélectionner.

Prévisualisez le déplacement avec le hash courant de la note :

```json
{
  "rel_path": "backlog.md",
  "heading": "BL-0123",
  "heading_level": 4,
  "destination_heading": "Archives du service",
  "destination_level": 3,
  "expected_hash": "<content_hash courant de la note>",
  "confirm": false
}
```

Vérifiez les coordonnées source et destination ainsi que `projected_hash`. Les lignes
de déplacement sont numérotées à partir de un dans le corps Markdown, hors frontmatter. Pour
appliquer le déplacement, rappelez l'opération avec `confirm: true` et un `request_id`
stable. La prévisualisation ne modifie ni les octets ni l'index. L'application exige
le même hash courant, conserve les anciens octets et réconcilie l'index.

La destination doit exister. Les niveaux de titres sont conservés ; le numéro de
niveau source doit être supérieur à celui de destination et permettre une insertion
comme dernier enfant direct. Les titres dupliqués exigent le niveau et l'occurrence
à partir de un (`heading_occurrence` ou `destination_occurrence`). L'outil refuse une
destination identique ou descendante, une position inchangée, des fins de ligne
mixtes, ainsi qu'un déplacement qui modifierait l'interprétation des titres Markdown
ou nécessiterait l'insertion d'un saut de ligne.

Les anciens tableaux d'archive ne sont pas convertis. Créez un titre d'archive adapté
avec les writers existants, puis déplacez les items sélectionnés. Les déplacements
entre notes restent des opérations par manifeste d'organisation.

## Mettre à jour un compteur de backlog

`set_frontmatter` accepte `last_id`, avec `expected_hash` obligatoire. Sa valeur est
`BL-` suivi d'au moins quatre chiffres décimaux ASCII. Un compteur valide existant
ne peut pas diminuer ; une valeur existante mal formée est refusée. Le corps reste
préservé.

Le passage d'un compteur en prose au frontmatter constitue une migration distincte :
lisez la valeur courante faisant foi, écrivez-la avec CAS, vérifiez le reçu, puis
remplacez l'ancien compteur en prose par un renvoi au frontmatter. Ne laissez jamais
deux compteurs concurrents. Utilisez un `request_id` stable par mutation et relisez
la note après chaque écriture.

Les mises à jour du compteur préservent le texte YAML non concerné ainsi que le corps.
Elles refusent les ancres, alias, clés dupliquées, fusionnées ou complexes, mappings
en ligne et suppressions de champs combinées, afin de préserver les autres métadonnées.
Les mises à jour sans `last_id` conservent leur sérialisation habituelle.

## Commencer par les extraits d'orientation utiles

`session_context` peut sélectionner des chemins de titres configurés dans les notes
d'orientation au lieu de renvoyer seulement leur début. `DATACRON_SESSION_CONTEXT_SECTIONS`
accepte un objet JSON associant chaque chemin de note à des listes de chemins de titres.
Ce réglage s'applique aux notes déjà chargées par `DATACRON_SESSION_CONTEXT_PATHS` ou une
autre sélection. Un objet vide conserve la lecture de la première page pour toutes les notes.

Aucune section n'est sélectionnée par défaut : une note d'orientation sans sections
configurées renvoie son début et indique `section_selection.mode` à `full` avec la raison
`no_sections_configured`. Le réglage est un objet JSON dont les clés sont des chemins de
notes relatifs au vault et les valeurs des listes de chemins de titres ; un chemin de titres
énumère les titres rendus depuis le titre de premier niveau jusqu'à la section choisie. Pour
une note `_memory/INIT.md` avec un titre de premier niveau `INIT` et deux sections de niveau
deux :

```json
{"_memory/INIT.md": [["INIT", "Où vivent les choses"], ["INIT", "Comment écrire"]]}
```

Chaque source sélectionnée expose un tableau `excerpts` contenant des extraits
encadrés séparément, le hash original de la note et les requêtes `next_read` permettant
de poursuivre les sections incomplètes. Le budget habituel par note est partagé entre
les extraits et le plafond global de réponse sérialisée reste appliqué. Aucun décalage
unique n'est utilisé pour représenter des extraits discontinus.

Un titre absent ou ambigu produit un résultat `full_fallback` explicite avec la première
page habituelle. Un sélecteur expurgé n'est pas proposé comme requête de continuation
utilisable. L'orientation ne répare pas l'index et ne modifie pas le vault.

## Corriger un sélecteur de titre introuvable

Les erreurs d'écriture conservent le code `heading_not_found` et proposent un nombre
borné de titres rendus exacts, avec leurs niveaux et occurrences. Les suggestions sont
assainies et ne sélectionnent jamais une section automatiquement. Une suggestion
tronquée ou expurgée est signalée comme impropre à une sélection directe ; relisez
le plan avant de choisir la cible. Les erreurs de `move_note_section` portent aussi
`selector` : `source` quand le titre à déplacer n'a pas été sélectionné, `destination`
quand le titre de destination est absent, ambigu ou sélectionné avec une occurrence
invalide. Une sélection ambiguë retourne le code `heading_ambiguous` avec un `next_action` ;
le message de destination nomme `destination_level` et `destination_occurrence`.
