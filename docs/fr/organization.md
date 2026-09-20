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

Le bloc porte cinq clés, et cinq seulement.

| Clé | Type | Rôle |
|---|---|---|
| `scope` | chaîne | Sous-arbre du vault sur lequel porte la mesure. |
| `rules` | liste | Règles de placement, dans l'ordre de priorité. |
| `tags` | dictionnaire | La [politique de tags](#le-bloc-tags--une-politique-de-tags-déclarée) optionnelle. |
| `state_note_min_notes` | entier, au moins 1 | A partir de combien de notes un dossier de sujet doit porter une note d'état. Absente : `NO_STATE_NOTE` n'est pas mesuré. |
| `linking_since` | date | A partir de quelle date calendaire une nouvelle note d'un dossier de sujet doit lier sa note d'état. Absente : `UNLINKED` n'est pas mesuré. |

`state_note_min_notes` et `linking_since` sont lues strictement : un booléen n'est pas un
compte, un nombre n'est pas une date, et l'une ou l'autre déclarée sans politique `tags`
portant un `subject_namespace` est une erreur au chargement qui nomme la clé, parce que les
mesures de dossier n'existent que sur les règles de sujet. **Mettre à niveau chaque
installation de Datacron avant de les déclarer** : un exécutable antérieur à cette version
refuse tout le `VAULT.yaml` avec une erreur de validation `extra_forbidden`, ce qui arrête
son serveur, exactement comme pour le bloc `tags`.

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
| `placement_namespace` | oui | L'espace de noms des tags de placement (`memory` quand les règles sont `memory/fact`, `memory/project`, ...). Chaque tag de règle y vit, ou nomme un sujet déclaré (voir [les règles de sujet](#les-règles-de-sujet)). |
| `markers` | non | Tags de règle qui peuvent accompagner le tag de placement comme marqueur transversal (`memory/decision` sur un fait qui tranche quelque chose). |
| `subject_namespace` | non | L'espace de noms qui nomme le sujet auquel une note appartient (`project`). |
| `subjects` | non | Le registre fermé des tags de sujet, chacun avec des `aliases` optionnels : les graphies à signaler plutôt qu'à accepter en silence. Une chaîne seule est un sujet sans alias. |
| `subject_exempt_tags` | non | Tags de placement dont les notes peuvent porter plusieurs sujets (une fiche personne se rattache à plusieurs projets). |
| `allowed_namespaces` | non | Les autres espaces de noms admis à côté de ceux du placement et du sujet (`org`, `meta`). Tout autre tag contenant `/` est refusé. |
| `archive_tags` | non | Tags qui marquent une note comme archivée pour le classement et pour la bibliothèque hors ligne. Défaut : `memory/archive`, `meta/archive`. Une liste vide ou `null` ne déclare aucun tag d'archive ; cela ne restaure pas le défaut. Un exécutable plus ancien que cette version refuse un `VAULT.yaml` qui déclare cette clé, comme il le fait pour tout le bloc `tags` : mettre à jour chaque installation d'abord. |

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
  message nomme chaque infraction et ce qui était attendu. Rien n'est écrit. La politique
  est lue au démarrage du serveur, avec le reste de `VAULT.yaml` : redémarre le serveur
  après l'avoir modifiée.
- `apply_organization_manifest` refuse, à la validation, un lot dont les notes
  **résultantes** enfreignent la politique de la configuration cible, avec le même code. Les
  notes que le lot ne touche pas ne sont pas jugées, pour qu'un nettoyage incrémental reste
  possible.
- `datacron reorganize` rapporte les trois natures d'écart décrites plus bas.

Un vault sans ce bloc n'est pas jugé ; rien de tout cela n'existe tant que le vault ne le
déclare pas. La politique exige au moins une règle de placement, chaque marqueur et chaque
tag exempté doit être un tag de règle de placement, les tags de règle doivent être en
minuscules (le résolveur de règles les compare exactement, la politique les compare en
minuscules), un alias ne peut pas entrer en collision avec un tag de règle, et une clé
inconnue est un échec bruyant au chargement, comme partout ailleurs dans le bloc.

### Les règles de sujet

Une fois la politique déclarée, une règle peut être portée par un **tag de sujet du
registre** au lieu d'un tag de placement : `project/heimdall` vers
`_memory/subjects/perso/heimdall`. La règle se comporte comme les autres, la première
déclarée qui correspond gagne, et elle porte le dossier, le gabarit de nom et le plafond des
notes qu'elle gouverne. Ce qui change, c'est la lecture de la note : le dossier dit à quel
sujet elle appartient, le tag de placement dit toujours ce qu'elle est.

- La politique ne change pas. Une note gouvernée par une règle de sujet porte toujours
  **exactement un tag de placement** et au plus un marqueur ; le tag de la règle de sujet est
  jugé comme un sujet, jamais compté comme tag de placement. Une note avec un tag de sujet et
  sans tag de placement est `UNGOVERNED`, même quand une règle de sujet décide de son dossier.
- Un tag de règle qui n'est ni dans l'espace de placement ni un sujet déclaré est refusé au
  chargement, de même qu'un alias utilisé comme tag de règle. Les marqueurs et les tags
  exemptés doivent nommer des règles de placement, et au moins une règle de placement doit
  rester.
- L'ordre de déclaration fait le reste. Déclarer les règles des types qui doivent rester
  ensemble (`memory/contact`, `memory/preference`, ...) **avant** les règles de sujet, et les
  règles de sujet **avant** les règles de placement qui servent de repli aux notes sans sujet.
  Une règle de sujet déclarée en premier attirerait chaque fiche personne dans le dossier du
  sujet.
- La règle gagnante porte tout : déclarer `max_kb` sur chaque règle de sujet, car le plafond
  de `memory/fact` ne s'applique pas à un fait gouverné par son sujet.

### Ce que la politique ne couvre pas

- **Seules la création et les manifestes sont gardés.** `append_journal`,
  `patch_note_section`, `patch_note_preamble`, `rename_note_section`, `delete_note_section`, `move_note_section`
  et `revert_note` modifient un corps sans rejuger ses tags : une entrée qui écrit
  `#topic/x` en prose, ou une version historique restaurée, peut dériver.
  `datacron reorganize` mesure cette dérive ; c'est le contrôle à lancer après ces
  écritures, pas une raison d'écrire des tags en ligne.
- **Les tags en ligne suivent les heuristiques de l'extracteur.** Un `#tag` dans un span à
  un backtick ou dans un bloc de code délimité symétrique est ignoré ; un tag dans un bloc de
  code indenté, ou dans un bloc fermé par un marqueur d'une autre longueur, compte encore.
  Un tag en ligne commence par une lettre ASCII ou `_` et continue par des lettres ASCII,
  des chiffres, `_`, `-` et `/` ; un tag accentué ou contenant un point est tronqué au
  premier autre caractère. Écrire les tags dans le frontmatter, jamais en prose.
- **Toute installation de Datacron doit être mise à niveau avant de déclarer le bloc.** Un
  exécutable antérieur à la politique refuse tout le `VAULT.yaml` avec une erreur de
  validation `extra_forbidden`, ce qui arrête son serveur ; la clé n'est pas ignorée.
- **Le rapport JSON liste neuf compteurs même sans politique.** Les trois compteurs de
  politique valent alors toujours zéro, et `--kind` accepte leurs noms ; l'identité
  `scanned = governed + unmatched + skipped` et tous les autres champs sont inchangés.

```yaml
organization:
  scope: _memory
  rules:
    - tag: memory/contact
      folder: _memory/people
    - tag: project/heimdall
      folder: _memory/subjects/perso/heimdall
      max_kb: 121
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

Neuf écarts sont rapportés, et rien d'autre. Les trois premiers mesurent une note gouvernée
contre sa règle ; les trois suivants n'existent que si le vault déclare une politique
`tags` ; les trois derniers mesurent les dossiers de sujet et les blocs de code. Chacun
compare le vault à une intention déclarée : le planner n'invente jamais un placement, un
lien ni une note.

| Nature | Signification |
|---|---|
| `WRONG_FOLDER` | La note n'est pas dans le dossier que sa règle déclare. |
| `NAMING` | Le stem ne satisfait pas le gabarit de sa règle. |
| `OVER_SIZE` | La note dépasse le `max_kb` de sa règle. |
| `UNGOVERNED` | La note ne porte aucun tag de placement (politique déclarée seulement). |
| `UNKNOWN_TAG` | Un tag est l'alias d'un sujet enregistré, un sujet hors registre, un espace de noms non déclaré, ou un tag `memory/*` qui n'est pas un tag de règle (politique déclarée seulement). |
| `TAG_CARDINALITY` | Plusieurs tags de placement, ou plusieurs tags de sujet sur une note dont le tag de placement n'est pas exempté (politique déclarée seulement). |
| `NO_STATE_NOTE` | Un dossier de sujet contient au moins `state_note_min_notes` notes et aucune ne porte un tag `kind/*` (clé déclarée seulement). |
| `UNLINKED` | Une note d'un dossier de sujet datée à partir de `linking_since` ne porte aucun wikilink vers la note d'état du dossier (clé déclarée seulement). |
| `UNBALANCED_FENCE` | Le corps d'une note gouvernée compte un nombre impair de lignes de délimitation de bloc de code. |

### Notes d'état et rattachements

Un **dossier de sujet** est le dossier d'une [règle de sujet](#les-règles-de-sujet) : une
règle dont le tag vit dans le `subject_namespace` de la politique. Les deux clés exigent
donc une politique `tags` avec un `subject_namespace` ; en déclarer une sans lui est refusé
au chargement plutôt que mesuré comme rien. Seules comptent les notes gouvernées qui sont
réellement dans le dossier de la règle ; une note mal placée est déjà `WRONG_FOLDER`.

La **note d'état** d'un dossier se reconnaît à un tag de l'espace `kind` (`kind/platform`,
`kind/development`, `kind/mission`), jamais à son stem. Un dossier peut en contenir
plusieurs ; n'importe laquelle satisfait un lien. `NO_STATE_NOTE` est rapporté une fois
par dossier : son `rel_path` est le dossier, son `detail` le nombre de notes, et il se
trie avec les écarts de notes par chemin.

`UNLINKED` s'applique aux notes que la convention demande de rattacher, et à elles
seulement : une note dont la date calendaire (`created`, puis `updated`, la même que pour
`{date}`) est égale ou postérieure à `linking_since`, qui n'est pas elle-même une note
d'état, et dont le stem ne contient pas `-history-` (une note d'historique scindée se
nomme `<sujet>-history-<periode>`). Le stock antérieur n'est pas repris. Une note est
rattachée quand l'une de ses cibles `[[...]]` nomme une note d'état de son dossier par son
**stem**, par son **titre** de frontmatter ou par l'un de ses **alias**, sans tenir compte
de la casse ; `[[cible#ancre]]` et `[[cible|libellé]]` comptent pour `cible`, un wikilink
dans un bloc de code délimité ne compte pas. Quand le dossier n'a pas de note d'état, aucun
`UNLINKED` n'est rapporté : le dossier est soit sous le seuil, soit déjà `NO_STATE_NOTE`.

`UNBALANCED_FENCE` n'a besoin d'aucune clé. Une ligne de délimitation est une ligne du
corps qui commence par trois accents graves après au plus trois espaces d'indentation ;
l'ouverture et la fermeture suivent la même règle, donc un corps équilibré en compte un
nombre pair. Une délimitation par tildes (`~~~`) est hors périmètre. L'écart compte parce
qu'une scission de note sur une ligne située dans un bloc de code laisse le reste de la
note non analysé : le sélecteur de sections ne voit plus les titres qui suivent, alors
que l'index reste sain.

La règle est une heuristique par ligne, et ses limites sont mesurées, pas garanties. Une
délimitation ouverte avec quatre accents graves ou plus qui contient une ligne à trois
accents graves, et une délimitation fermante suivie de texte sur la même ligne, sont
comptées par la même règle, de sorte qu'un tel corps peut être rapporté comme
déséquilibré ou passer pour équilibré. Un corps dont la toute première ligne est une
délimitation indentée de quatre espaces est mesuré comme non indenté, parce que l'analyseur
de frontmatter retire les espaces de tête du corps. L'exclusion des wikilinks suit la même
parité, donc un lien dans un tel bloc peut encore compter comme un lien. Les fins de ligne
sont ramenées à LF avant le comptage, de sorte qu'une note en CRLF se mesure de la même
façon depuis le système de fichiers et depuis un payload de manifeste.

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
datacron reorganize --vault G:\mon-vault --dry-run --freshness-days 60
```

| Option | Rôle |
|---|---|
| `--vault`, `-v` | Racine du vault. Repli : `DATACRON_VAULT_ROOT`, puis le répertoire courant s'il contient un `VAULT.yaml` sous `.datacron`. |
| `--dry-run` | **Obligatoire.** Aucun autre mode n'existe, et le drapeau ne doit jamais devenir implicite. |
| `--json` | Rapport machine stable au lieu du texte. |
| `--kind` | Restreint le rapport à une nature : `WRONG_FOLDER`, `NAMING`, `OVER_SIZE`, `UNGOVERNED`, `UNKNOWN_TAG`, `TAG_CARDINALITY`, `NO_STATE_NOTE`, `UNLINKED` ou `UNBALANCED_FENCE`. |
| `--freshness-days N` | Liste en plus les notes d'état dont `last_verified` manque ou date de plus de `N` jours (entier positif). Informatif : le code de sortie ne change pas. |

`--dry-run` est exigé explicitement. Omis, la commande refuse de s'exécuter. Une valeur de
`--kind` inconnue liste les valeurs attendues.

### Fraîcheur des notes d'état

Une session qui vérifie une note d'état renseigne sa clé de frontmatter `last_verified`.
Avec `--freshness-days N`, le rapport liste chaque note gouvernée portant un tag `kind/*`
dont `last_verified` manque, ou date de plus de `N` jours par rapport à la date
d'exécution (date calendaire UTC). Une note vérifiée il y a exactement `N` jours n'est pas
listée. La liste est triée par chemin et ne change jamais le code de sortie : c'est une
aide à la lecture, pas un écart.

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
  WRONG_FOLDER     0
  NAMING           0
  OVER_SIZE        0
  UNGOVERNED       0
  UNKNOWN_TAG      0
  TAG_CARDINALITY  0
  NO_STATE_NOTE    0
  UNLINKED         0
  UNBALANCED_FENCE 0
No deviation found.
```

Avec `--freshness-days 60`, un bloc suit les compteurs, une ligne par note d'état
périmée :

```text
Freshness (older than 60 days): 2
  knowledge/subjects/alpha/alpha.md (project/alpha, 74 days)
  knowledge/subjects/beta/beta.md (project/beta, never verified)
```

## Le contrat JSON

`--json` rend un document dont le schéma est identifié par `organization-plan-v2`. La
sérialisation est déterministe : indentation de deux espaces, clés triées, caractères
non-ASCII conservés tels quels. La version 2 ajoute les trois compteurs de dossier et de
bloc de code et le champ optionnel `freshness` ; chaque champ de la version 1 garde son
sens.

| Champ | Contenu |
|---|---|
| `schema` | `organization-plan-v2` |
| `vault_root` | Racine mesurée |
| `scope` | Portée déclarée |
| `scanned` | Notes admises dans la portée |
| `governed` | Notes qu'une règle réclame |
| `unmatched` | Notes admises qu'aucune règle ne réclame |
| `counts` | Nombre d'écarts par nature, une clé par nature ; les clés sont triées alphabétiquement dans le document sérialisé, et l'ordre déclaré des natures s'applique au rapport texte |
| `deviations` | Liste d'écarts : `rel_path`, `kind`, `tag`, `detail`, `expected` |
| `skipped` | Notes illisibles : `rel_path`, `reason` |
| `freshness` | Présent seulement avec `--freshness-days` : `rel_path`, `tag`, `last_verified`, `age_days` (`null` quand la date manque) |

L'identité `scanned = governed + unmatched + skipped` est toujours vraie : une note que le
planner ne peut pas lire est balayée, puis ignorée, et n'est ni gouvernée ni hors règle.
Un écart de dossier (`NO_STATE_NOTE`) a le dossier pour `rel_path` ; il ne change aucun
des compteurs.

```json
{
  "counts": {
    "NAMING": 0,
    "NO_STATE_NOTE": 0,
    "OVER_SIZE": 0,
    "TAG_CARDINALITY": 0,
    "UNBALANCED_FENCE": 0,
    "UNGOVERNED": 0,
    "UNKNOWN_TAG": 0,
    "UNLINKED": 0,
    "WRONG_FOLDER": 0
  },
  "deviations": [],
  "governed": 391,
  "scanned": 392,
  "schema": "organization-plan-v2",
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

Le rapport projeté qu'une validation signe est le même rapport à neuf natures que
`reorganize` rend, calculé à partir des octets des payloads du lot : un payload qui
introduit un bloc de code déséquilibré ou retire un lien se voit dans
`projected_report_sha256` avant toute écriture. La projection n'a pas de date
d'exécution, elle ne porte donc jamais le champ `freshness` ; le rapport final après
application est calculé sans lui aussi, et les deux empreintes coïncident.

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
