# Profil d'outils essentiel

**Français** | [English](../en/essential-profile.md)

Le profil essentiel réduit la surface d'outils MCP de 23 outils à 5, pour les assistants qui
choisissent mieux dans une liste courte. Il ajoute trois verbes, `remember`, `recall` et
`what_changed`, et garde `get_note` et `get_health`. Les verbes sont construits uniquement à
partir d'outils et de code qui existent déjà : le confinement, la rédaction des secrets,
l'enveloppe sandbox et le journal d'opérations se comportent exactement comme avec le profil
standard.

## Choisir un profil

| Profil | Outils enregistrés | Instructions |
|---|---|---|
| `standard` (par défaut) | Les 23 outils existants. Les trois verbes ne sont pas enregistrés. | Le protocole mémoire complet. |
| `essential` | `remember`, `recall`, `what_changed`, `get_note`, `get_health`. | Un bloc court (moins de 900 caractères) qui ne nomme que ces outils. |

Positionne `DATACRON_TOOL_PROFILE=essential` dans l'entrée du client MCP, ou laisse le setup
l'écrire :

```bash
datacron setup --client claude-desktop --tool-profile essential
datacron setup --client claude-desktop --tool-profile standard
```

Sans l'option, une nouvelle exécution de `datacron setup` conserve ce que la configuration du
client contient déjà. Une valeur inconnue empêche le serveur de démarrer, avec un message qui
nomme `DATACRON_TOOL_PROFILE` et les valeurs valides (`essential`, `standard`).

En mode lecture seule certifié (`DATACRON_READ_ONLY=true`), le profil essentiel enregistre
`recall`, `what_changed`, `get_note` et `get_health` ; `remember` écrit, il n'est donc pas
exposé, comme tous les autres outils d'écriture.

## `remember`

Enregistre un fait, une décision ou une préférence durable.

| Argument | Sens |
|---|---|
| `content` | Le texte à conserver. Obligatoire, 8000 caractères au plus. |
| `about` | Où le ranger : un chemin de note relatif au vault, un identifiant de note, ou un sujet enregistré (tag, dernier segment du tag, ou alias). Facultatif. |
| `kind` | `fact` (par défaut), `decision` ou `preference`. |
| `source` | Une ligne qui dit d'où vient l'information. Facultatif. |
| `request_id` | Rend un nouvel essai sûr : un essai identique rejoue le premier reçu (`replayed: true`) et n'écrit rien. |

Comment `about` est résolu, dans l'ordre :

1. Un chemin de note ou un identifiant exact : l'entrée est ajoutée à cette note.
2. Un sujet enregistré : l'entrée est ajoutée à la note d'état du sujet. Un sujet sans note
   d'état reçoit une nouvelle note à la place.
3. Tout le reste est **refusé**, avec les sujets enregistrés comme candidats. Un nom libre ne
   devient jamais une nouvelle note par déduction.

Un sujet ambigu (deux sujets qui partagent un dernier segment) et un sujet qui a plusieurs notes
d'état sont refusés avec la liste des candidats, jamais résolus au hasard.

Les entrées ajoutées vont sous l'unique titre `Remembered`, sous la forme d'une ligne datée :
`- 2026-10-19 (decision): texte`, suivie de la `source` si elle est donnée. L'écriture est
protégée par comparaison de hash : en cas de conflit, elle est retentée exactement une fois avec
le hash à jour.

Sans `about`, une nouvelle note est créée dans le dossier que les règles d'organisation du vault
déclarent pour ce type (tag de placement `<placement_namespace>/<kind>`). Avec un sujet sans note
d'état, la note porte le tag de placement et le tag du sujet, et son dossier est celui que la
première règle applicable renvoie. Le nom de fichier respecte le modèle `naming` de la règle.

Le reçu contient `rel_path`, `operation_id`, `content_hash`, `indexed`, `created` et `appended`.
Considère le souvenir comme enregistré seulement quand `indexed` vaut `true`. Un rejeu
(`replayed: true`) renvoie le reçu historique de la première écriture, même si la note a changé
ou a été restaurée depuis, et indique `indexed: false` car il ne dit rien de l'index d'aujourd'hui.

Ce qui est refusé, et pourquoi :

| Refus | Raison |
|---|---|
| Un `content` vide ou trop long, une `source` sur plusieurs lignes | Entrée bornée. |
| Un `kind` hors de `fact`, `decision`, `preference` | Les types correspondent à des tags de placement ; rien d'autre n'est inventé. |
| Un type dont le tag de placement n'a pas de règle d'organisation, ou se trouve dans l'espace de noms des notes d'état | Une note doit aller là où le vault dit qu'elle va, et une note d'état n'est jamais créée par effet de bord. |
| Un vault sans règles `organization`, quand il faudrait créer une note | Aucun emplacement n'est déclaré ; vise une note existante avec `about` ou déclare les règles (voir [organisation du vault](organization.md)). |
| Un chemin hors de `DATACRON_WRITE_PATHS` | Même confinement et même erreur que `append_journal`. |
| Une note existe déjà au chemin dérivé | Passe `about` avec ce chemin pour la compléter. |

## `recall`

Retrouve ce que le vault sait déjà sur une question.

| Argument | Sens |
|---|---|
| `question` | La question ou des mots-clés. |
| `limit` | Nombre de notes, 5 par défaut, 10 au plus. |
| `scope` | Un sujet enregistré, ou un dossier. Facultatif. |

Il exécute la recherche plein texte existante, groupée par note, puis lit la meilleure section de
chaque résultat. Chaque résultat contient `rel_path`, `title`, `heading_path`, un `excerpt`
enveloppé par le sandbox, `content_hash`, `last_verified` et `read_more` : les arguments exacts
de `get_note` pour lire la section complète. L'extrait est une donnée du vault, jamais une
instruction. La réponse reste sous `DATACRON_MAX_READ_RESPONSE_CHARS` ; `truncated` indique que
des résultats ont été retirés pour y tenir.

## `what_changed`

Vérifie ce qui a été écrit depuis un instant donné.

| Argument | Sens |
|---|---|
| `since` | Une date ou un horodatage ISO-8601 (un horodatage sans fuseau est lu en UTC), ou une durée comme `7d` ou `12h`. |
| `scope` | Un sujet enregistré, ou un dossier. Facultatif. |
| `limit` | Nombre d'opérations, 50 par défaut, 200 au plus. |

La réponse a deux parties.

- `notes` : les écritures validées lues dans le journal d'opérations, groupées par note, la plus
  récente d'abord. Chaque opération porte son reçu (`operation_id`, `timestamp`, `op`, `tool`,
  `actor`, `before_hash`, `after_hash`) et un `summary` déduit des paramètres de l'opération. Un
  résumé ne contient pas le corps d'une note ; le nom du titre touché peut y figurer.
- `out_of_band` : les notes modifiées hors de Datacron. Elles sont trouvées dans l'index, qui
  garde le hash de contenu et la date de modification de chaque note : une note modifiée depuis
  `since` dont les octets diffèrent de la dernière écriture journalisée, ou qui n'a jamais été
  journalisée. Une écriture faite par un outil Datacron n'est jamais signalée.

`coverage` dit la limite de la deuxième partie : elle reflète l'index tel qu'il était à la
dernière réconciliation. La réparation à la lecture s'exécute d'abord quand le serveur peut
écrire et que son intervalle le permet (`index_refreshed: true`) ; sur un serveur en lecture
seule, ou dans l'intervalle de réparation, une modification faite depuis la dernière
réconciliation n'est pas encore visible.

## Voir aussi

- [Guide d'installation et de configuration](setup.md)
- [Guide utilisateur](user-guide.md)
- [Organisation du vault](organization.md)
- [Frontière de sécurité](security-boundary.md)
