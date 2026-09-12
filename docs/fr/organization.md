# Organisation du vault

**Français** | [English](../en/organization.md)

Un vault peut déclarer où ses notes appartiennent, comment elles doivent être nommées et
quelle taille elles ne doivent pas dépasser. Datacron mesure l'écart entre cette
déclaration et l'état réel du vault. La mesure est en lecture seule : elle ne déplace, ne
renomme et ne réécrit jamais une note.

Datacron connaît la *forme* d'une règle et rien d'autre. Les noms de dossiers et les noms
de tags viennent du sidecar du vault, jamais de ce paquet. Aucune taxonomie n'est fournie,
suggérée ni attendue : deux vaults aux conventions opposées sont servis identiquement.

Un vault sans bloc `organization` n'est pas affecté. La fonctionnalité est entièrement
facultative.

## Le modèle en une minute

Une règle associe **un tag** à **un dossier**, avec un gabarit de nom optionnel et un
plafond de taille optionnel. Une note est gouvernée par la première règle déclarée dont le
tag figure sur elle. Une note qu'aucune règle ne réclame n'est pas en faute : elle est hors
périmètre.

```text
tags de la note  ->  première règle déclarée qui correspond  ->  dossier + nom attendus
                                                             ->  écart mesuré, jamais corrigé
```

## Le bloc `organization` de `.datacron/VAULT.yaml`

Le bloc porte deux clés, et deux seulement.

| Clé | Type | Rôle |
|---|---|---|
| `scope` | chaîne | Sous-arbre du vault sur lequel porte la mesure. |
| `rules` | liste | Règles de placement, dans l'ordre de priorité. |

`scope` est **obligatoire dès qu'au moins une règle est déclarée**. Une liste de règles sans
portée est une erreur de configuration, pas une portée implicite couvrant tout le vault.

`scope` doit être relatif au vault. Un chemin absolu, un `:` ou un segment `..` ou `.` sont
refusés. La portée doit exister et être un répertoire.

Une clé inconnue dans le bloc est une erreur bruyante au chargement. C'est délibéré : une
clé mal orthographiée acceptée en silence laisserait une configuration qui paraît active et
ne mesure rien.

## Une règle

| Clé | Obligatoire | Défaut | Rôle |
|---|---|---|---|
| `tag` | oui | - | Le tag qui déclenche la règle. |
| `folder` | oui | - | Le dossier attendu, relatif au vault. |
| `naming` | non | `{slug}` | Gabarit du nom de fichier. |
| `max_kb` | non | aucun plafond | Taille maximale, entier strictement positif. |

**`max_kb` compte en kibioctets de 1024 octets.** `max_kb: 80` autorise donc 81920 octets.

`folder` est **relatif au vault**, pas à la portée, mais il doit néanmoins résoudre à
l'intérieur de la portée. Avec `scope: knowledge`, un `folder: knowledge/meetings` est
valide et un `folder: archive` est refusé. Les antislashs sont normalisés en barres
obliques ; un chemin absolu, un `:` ou une traversée de répertoire sont refusés.

Deux règles ne peuvent pas déclarer le même `tag`.

Comme pour le bloc, une clé inconnue dans une règle est refusée au chargement. Le risque
évité est précis : une règle dont la clé est mal écrite matcherait sans rien contraindre.

### L'ordre est la priorité

L'ordre de déclaration est normatif. La première règle dont le tag est présent sur la note
gagne, et la recherche s'arrête là.

C'est le seul départage pour une note qui porte plusieurs tags gouvernés à la fois, et il se
contrôle en réordonnant la liste, sans lire le code.

## Les gabarits de nom

Trois tokens existent, et trois seulement : `{slug}`, `{date}` et `{iso_date}`. Un token
inconnu est une erreur au chargement, dont le message liste les tokens permis. Un `naming`
vide est refusé.

Le gabarit est évalué sur le **stem**, c'est-à-dire le nom de fichier sans son extension
`.md`. Le texte littéral entre les tokens est échappé : un gabarit reste un gabarit et ne
devient jamais une expression régulière accidentelle.

| Token | Reconnaît | Lien avec le frontmatter |
|---|---|---|
| `{slug}` | `[^/\\]+`, tout sauf un séparateur de chemin | aucun |
| `{date}` | la date calendaire de la note | `created`, puis repli sur `updated` |
| `{iso_date}` | une date ASCII `YYYY-MM-DD` calendairement valide | **aucun** |

`{slug}` est délibérément permissif : ni slugification, ni contrainte de casse. Un gabarit
réduit à `{slug}` seul ne contraint donc rien. C'est ce qui l'entoure qui contraint.

La distinction entre les deux tokens de date est la seule subtilité de ce modèle, et elle
compte :

- `{date}` est **comparé au frontmatter**. Le nom doit porter la date de `created`, ou celle
  de `updated` si `created` est absent ou illisible. Un fichier daté d'un autre jour est un
  écart de nommage.
- `{iso_date}` est **structurel**. Il exige une date réelle, mais ne la compare à rien : ni
  au jour courant, ni à `created`, ni à `updated`. Toute date valide passe.

Contrainte de gabarit sur `{iso_date}` : un gabarit en contient au plus un, et s'il en
contient un, il doit commencer par lui.

## Le bloc `tags` : une politique de tags déclarée

Les règles de placement disent où va une note gouvernée. Elles ne disent rien d'une note
sans tag gouverné, d'une note qui en porte deux, d'un nom de sujet écrit de trois façons, ou
d'un espace de noms inventé par un client. Le bloc optionnel `tags`, à l'intérieur
d'`organization`, ferme cette brèche, et il est appliqué là où cela compte : à l'écriture,
pas seulement dans un rapport.

| Clé | Obligatoire | Rôle |
|---|---|---|
| `placement_namespace` | oui | L'espace de noms des tags de placement, qui sont les tags des règles (`memory` quand les règles sont `memory/fact`, `memory/project`, ...). Chaque tag de règle doit y vivre. |
| `markers` | non | Tags de règle qui peuvent accompagner le tag de placement comme marqueur transversal (`memory/decision` sur un fait qui tranche quelque chose). |
| `subject_namespace` | non | L'espace de noms qui nomme le sujet auquel une note appartient (`project`). |
| `subjects` | non | Le registre fermé des tags de sujet, chacun avec des `aliases` optionnels : les graphies à signaler plutôt qu'à accepter en silence. Une chaîne seule est un sujet sans alias. |
| `subject_exempt_tags` | non | Tags de placement dont les notes peuvent porter plusieurs sujets (une fiche personne se rattache à plusieurs projets). |
| `allowed_namespaces` | non | Les autres espaces de noms admis à côté de ceux du placement et du sujet (`org`, `meta`). Tout autre tag contenant `/` est refusé. |

La politique se lit ainsi. Une note porte **exactement un tag de placement** parmi les tags
de règle, éventuellement accompagné d'un marqueur ; **au plus un tag de sujet** pris dans le
registre, sauf si son tag de placement est exempté ; **aucun alias** d'un sujet enregistré,
nu (`heimdall`) ou préfixé (`projet/heimdall`) ; **aucun espace de noms non déclaré**, et
aucun tag `memory/*` qui ne soit pas un tag de règle. Les tags sans `/` qui ne sont pas des
alias sont des descripteurs libres et passent toujours. La comparaison ignore la casse, et
les occurrences `#tag` du corps comptent, de sorte qu'un frontmatter conforme ne peut pas
être défait par la prose.

Trois surfaces appliquent la même évaluation :

- `create_note_ai` refuse une note de la portée dont les tags effectifs enfreignent la
  politique, avec une erreur typée dont le `code` est `tag_policy_violation` et dont le
  message nomme chaque infraction et ce qui était attendu. Rien n'est écrit.
- `apply_organization_manifest` refuse, à la validation, un lot dont les notes
  **résultantes** enfreignent la politique de la configuration cible, avec le même code. Les
  notes que le lot ne touche pas ne sont pas jugées, pour qu'un nettoyage incrémental reste
  possible.
- `datacron reorganize` rapporte les trois natures d'écart décrites plus bas.

Un vault sans ce bloc n'est pas affecté ; rien de tout cela n'existe tant que le vault ne le
déclare pas. La politique exige au moins une règle, chaque marqueur et chaque tag exempté
doit être un tag de règle, et une clé inconnue est un échec bruyant au chargement, comme
partout ailleurs dans le bloc.

```yaml
organization:
  scope: _memory
  rules:
    - tag: memory/contact
      folder: _memory/people
    - tag: memory/fact
      folder: _memory/facts
      naming: "{iso_date}-{slug}"
    - tag: memory/decision
      folder: _memory/decisions
      naming: "{iso_date}-{slug}"
  tags:
    placement_namespace: memory
    markers: [memory/decision]
    subject_namespace: project
    subjects:
      - tag: project/heimdall
        aliases: [heimdall, projet/heimdall]
      - project/datacron
    subject_exempt_tags: [memory/contact]
    allowed_namespaces: [org, meta]
```

## Ce qui est mesuré, et ce qui ne l'est pas

Six écarts sont rapportés, et rien d'autre. Les trois premiers mesurent une note gouvernée
contre sa règle ; les trois derniers n'existent que si le vault déclare une politique `tags`.

| Nature | Signification |
|---|---|
| `WRONG_FOLDER` | La note n'est pas dans le dossier que sa règle déclare. |
| `NAMING` | Le stem ne satisfait pas le gabarit de sa règle. |
| `OVER_SIZE` | La note dépasse le `max_kb` de sa règle. |
| `UNGOVERNED` | La note ne porte aucun tag de placement (politique déclarée seulement). |
| `UNKNOWN_TAG` | Un tag est l'alias d'un sujet enregistré, un sujet hors registre, un espace de noms non déclaré, ou un tag `memory/*` qui n'est pas un tag de règle (politique déclarée seulement). |
| `TAG_CARDINALITY` | Plusieurs tags de placement, ou plusieurs tags de sujet sur une note dont le tag de placement n'est pas exempté (politique déclarée seulement). |

**Sans politique `tags`, une note qu'aucune règle ne réclame n'est pas un écart.** Elle est
comptée dans `unmatched`, et Datacron ne lui invente jamais un placement. C'est une
propriété du modèle, pas une tolérance : un vault peut contenir autant de notes non
gouvernées qu'il le souhaite. Avec une politique déclarée, une telle note reste comptée dans
`unmatched` et est en plus rapportée comme `UNGOVERNED`, de sorte que l'identité ci-dessous
reste vraie.

Une note que le planner ne parvient pas à lire est reportée dans `skipped` avec son motif.
Elle n'interrompt jamais le balayage.

Les écarts sont triés par chemin puis par nature, jamais par l'ordre de parcours du
système de fichiers : deux exécutions sur un vault inchangé rendent le même rapport.

### Quels tags comptent

Les tags effectifs d'une note agrègent deux sources : le champ `tags` du frontmatter **et**
les occurrences `#tag` présentes dans le corps. Ils sont mis en minuscules, dédupliqués, et
conservent l'ordre de première apparition.

Les tags de prose ayant exactement la forme d'une couleur hexadécimale sont écartés. Les
tags du frontmatter ne sont jamais filtrés.

Conséquence à connaître : un `#tag` écrit dans le corps participe à la résolution. Il peut
donc changer la règle gagnante, et par là le placement attendu, selon les autres tags
présents et l'ordre des règles.

## Mesurer : `datacron reorganize`

```text
datacron reorganize --vault G:\mon-vault --dry-run
datacron reorganize --vault G:\mon-vault --dry-run --json
datacron reorganize --vault G:\mon-vault --dry-run --kind NAMING
```

| Option | Rôle |
|---|---|
| `--vault`, `-v` | Racine du vault. Repli : `DATACRON_VAULT_ROOT`, puis le répertoire courant s'il contient un `VAULT.yaml` sous `.datacron`. |
| `--dry-run` | **Obligatoire.** Aucun autre mode n'existe, et le drapeau ne doit jamais devenir implicite. |
| `--json` | Rapport machine stable au lieu du texte. |
| `--kind` | Restreint le rapport à une nature : `WRONG_FOLDER`, `NAMING` ou `OVER_SIZE`. |

`--dry-run` est exigé explicitement. Omis, la commande refuse de s'exécuter. Une valeur de
`--kind` inconnue liste les valeurs attendues.

Sans règle déclarée, la commande ne rapporte pas une erreur : elle indique qu'il n'y a rien
à mesurer.

### Codes de sortie

| Code | Signification |
|---|---|
| `0` | Aucun écart. |
| `1` | Le rapport n'est pas vide. |
| `2` | Le vault ou sa configuration n'a pas pu être lu. |

**`1` n'est pas une erreur.** La séparation entre `1` et `2` existe pour qu'un rapport non
vide reste détectable en intégration continue sans faire échouer le job pour une mauvaise
raison. Une configuration invalide, une portée absente ou un vault illisible donnent `2`.

### Sortie texte

```text
Organization report for G:\mon-vault
  scanned 392 notes, 391 governed, 1 out of scope
  WRONG_FOLDER   0
  NAMING         0
  OVER_SIZE      0
  UNGOVERNED     0
  UNKNOWN_TAG    0
  TAG_CARDINALITY 0
No deviation found.
```

## Le contrat JSON

`--json` rend un document dont le schéma est identifié par `organization-plan-v1`. La
sérialisation est déterministe : indentation de deux espaces, clés triées, caractères
non-ASCII conservés tels quels.

| Champ | Contenu |
|---|---|
| `schema` | `organization-plan-v1` |
| `vault_root` | Racine mesurée |
| `scope` | Portée déclarée |
| `scanned` | Notes admises dans la portée |
| `governed` | Notes qu'une règle réclame |
| `unmatched` | Notes admises qu'aucune règle ne réclame |
| `counts` | Nombre d'écarts par nature |
| `deviations` | Liste d'écarts : `rel_path`, `kind`, `tag`, `detail`, `expected` |
| `skipped` | Notes illisibles : `rel_path`, `reason` |

L'identité `scanned = governed + unmatched` est toujours vraie.

```json
{
  "counts": {
    "NAMING": 0,
    "OVER_SIZE": 0,
    "TAG_CARDINALITY": 0,
    "UNGOVERNED": 0,
    "UNKNOWN_TAG": 0,
    "WRONG_FOLDER": 0
  },
  "deviations": [],
  "governed": 391,
  "scanned": 392,
  "schema": "organization-plan-v1",
  "scope": "knowledge",
  "skipped": [],
  "unmatched": 1,
  "vault_root": "G:\\mon-vault"
}
```

## Mesurer, puis appliquer

Les deux moitiés de la fonctionnalité sont séparées, et l'ordre est le bon sens :

- `datacron reorganize` **mesure** l'écart. Il est en lecture seule, et ne propose ni plan
  d'action ni commande à lancer.
- Le tool MCP `apply_organization_manifest` **applique** un lot adressé par contenu, en deux
  temps : `mode="validate"` rend un jeton lié à l'état exact admis, puis `mode="apply"`
  n'agit que si ce jeton exact lui est présenté.

Voir le [guide utilisateur](user-guide.md) pour l'usage du tool, et la
[santé opérationnelle](operational-health.md) pour la fenêtre de maintenance qu'une
application exige.

## Exemple complet

Cet exemple est une **illustration**, pas un défaut ni une recommandation. Les tags et les
dossiers ci-dessous ne sont fournis par Datacron d'aucune manière : ils viennent
entièrement du vault qui les déclare.

```yaml
organization:
  scope: knowledge
  rules:
    - tag: kind/meeting
      folder: knowledge/meetings
      naming: "{iso_date}-{slug}"
      max_kb: 64
    - tag: kind/journal
      folder: knowledge/journal
      naming: "{date}-{slug}"
    - tag: kind/reference
      folder: knowledge/reference
      naming: "{slug}"
```

Lecture de cet exemple :

- une note portant `kind/meeting` doit vivre dans `knowledge/meetings`, s'appeler
  `2026-08-31-revue-trimestre.md` par exemple, et peser au plus 65536 octets ;
- une note portant `kind/journal` doit porter la date de son propre frontmatter, ce qui est
  une contrainte plus forte que la précédente ;
- une note portant `kind/reference` n'est contrainte que sur son dossier ;
- une note portant à la fois `kind/meeting` et `kind/reference` est gouvernée par
  `kind/meeting`, parce que cette règle est déclarée en premier ;
- une note ne portant aucun de ces trois tags est hors périmètre, et n'apparaît que dans
  `unmatched`.

## Pour aller plus loin

- [Conventions du vault (SPEC)](spec.md) : contrat du sidecar `.datacron/` et du frontmatter.
- [Guide utilisateur](user-guide.md) : usage quotidien depuis Claude.
- [Santé opérationnelle](operational-health.md) : durabilité, `get_health`, fenêtres de maintenance.
