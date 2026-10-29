# Runbooks

**Français** | [English](../en/runbooks.md)

Un runbook, c'est un dossier par sujet, un fichier Markdown par thème numéroté
(`00-architecture`, `50-troubleshooting`, `90-variables`, etc.) et un dossier
`quickref/` de fiches de survie par service. Un vault peut déclarer où vivent ses
runbooks et à quoi ils ressemblent ; `runbook_capture` range alors chaque fait
durable dans le bon runbook et la bonne section, dès que le travail l'établit.

## Le bloc `runbooks`

Le bloc est facultatif. Un vault qui n'en a pas se comporte exactement comme avant.
Les clés inconnues sont refusées au démarrage : une clé mal orthographiée échoue
bruyamment au lieu d'être ignorée.

```yaml
runbooks:
  roots:
    - folder: CPDC                       # relatif au vault
      directory_naming: "{slug}-runbook" # valeur par défaut
      template: infra
  templates:
    infra:
      language: en
      readme: README
      observation_label: On Mets
      roles:
        architecture:    {stem: 00-architecture,    match: '^\d{2}-architecture$', title: Architecture}
        troubleshooting: {stem: 50-troubleshooting, match: '^\d{2}-troubleshooting$', title: Troubleshooting}
        escalation:      {stem: 60-escalation,      match: '^\d{2}-.*escalation$', title: Escalation}
        variables:       {stem: 90-variables,       match: '^\d{2}-variables$', title: Variables}
      quickref: {folder: quickref, language: fr}
      labels:
        symptom: Symptom
        cause: Cause
        fix: Fix
        not_captured: Not captured
        update: Update
        machine: Machine
        output: Output
        placeholder: Placeholder
        meaning: Meaning
        value: On Mets
        decisions: Decisions
        reason: Reason
        discarded: Discarded options
        open_questions: Open questions
        owner: Owner
      routing:
        error_fix: troubleshooting
        contact: escalation
        server: variables
        variable: variables
        decision: architecture
        fact: architecture
```

### Racines

| Clé | Sens |
|---|---|
| `folder` | Dossier relatif au vault qui contient des runbooks. Il ne doit pas se trouver dans `knowledge/`, et aucun de ses segments ne peut être un répertoire exclu (voir plus bas). Deux racines ne doivent pas se chevaucher. |
| `directory_naming` | Nom d'un dossier de runbook sous la racine. Il contient `{slug}` une seule fois plus une partie fixe, pour que tout sous-dossier de la racine ne soit pas un runbook. Par défaut `{slug}-runbook`. |
| `template` | Nom d'un gabarit déclaré. |

### Gabarits

| Clé | Sens |
|---|---|
| `language` | Langue des fichiers numérotés (`en`, `fr`). Chaque reçu la rappelle, pour que l'appelant écrive l'entrée dans cette langue. |
| `readme` | Nom du fichier d'index, sans `.md`. Aucun rôle ne peut l'utiliser ; la capture ne l'écrit jamais. |
| `roles` | Une entrée par rôle de fichier (identifiant en minuscules). |
| `quickref` | Facultatif : `folder`, sa propre `language`, et `naming` avec `{service}` une seule fois (par défaut `{service}`). Le dossier est réservé ; la capture n'écrit pas encore de fiche de survie. |
| `observation_label` | Libellé des encadrés d'observation datés, par exemple `On Mets`. |
| `labels` | Chaque mot que Datacron écrit, dans la langue du gabarit. Une capture dont le type demande un libellé que le gabarit ne déclare pas est refusée, en le nommant. Datacron n'embarque aucun texte de runbook. |
| `routing` | Type de capture vers rôle : `error_fix`, `command`, `server`, `variable`, `contact`, `decision`, `fact`, `open_question`. Un type absent n'a pas de rôle par défaut, et une capture de ce type doit en nommer un. |

### Rôles

Un rôle se trouve par le motif auquel répond le nom de son fichier, pas par son
numéro : le runbook WSUS range le troubleshooting dans `50-troubleshooting`, le
runbook AD dans `70-troubleshooting`, et les deux répondent à
`^\d{2}-troubleshooting$`.

| Clé | Sens |
|---|---|
| `stem` | Nom de fichier qu'utilise une création, sans `.md`. Il doit répondre à son propre `match`, et au motif d'aucun autre rôle, sinon la capture suivante ne retrouverait pas le fichier. |
| `match` | Expression régulière appliquée au nom entier. Un motif à répétition imbriquée est refusé. |
| `title` | Le titre `# ` d'un fichier que ce rôle crée. Sans lui, la capture refuse de créer le fichier et n'écrit que dans un fichier existant. |

## Capturer un fait : `runbook_capture`

L'outil n'est enregistré que si le vault déclare `runbooks`, que
`DATACRON_RUNBOOK_WRITE_PATHS` est défini, que le serveur n'est pas en lecture seule
et que le profil d'outils est `standard`. Sinon, la liste des outils ne change pas.

| Argument | Sens |
|---|---|
| `subject` | Un sujet enregistré (tag, dernier segment ou alias), le slug d'un runbook existant, ou le chemin d'un dossier de runbook comme `CPDC/wsus-runbook`. |
| `kind` | L'un des huit types ci-dessous. |
| `evidence` | `measured` (une commande réellement passée, sa sortie réelle), `said` (une réunion datée ou une personne nommée) ou `unverified`. |
| `source` | Une ligne qui dit d'où vient le fait. Elle est gardée dans le journal d'opérations, pas dans le runbook. |
| `fields` | Les champs du type, ci-dessous. Un champ inconnu est refusé. |
| `observed_on` | Date de l'événement, `AAAA-MM-JJ`, jamais dans le futur. Par défaut : aujourd'hui (UTC). |
| `target_role` | Un rôle qui remplace le routage. |
| `dry_run` | `true` renvoie le plan sans rien écrire. |
| `request_id` | Rend une reprise sûre : une reprise identique rejoue le premier reçu. |

| Type | Champs requis | Champs facultatifs | Écrit sous la forme |
|---|---|---|---|
| `error_fix` | `symptom`, `fix` | `cause` | Un H2 `Symptom: <symptom>` avec `Cause`, `Fix` et la provenance. Une cause absente s'écrit avec le libellé `not_captured`. |
| `command` | `machine`, `heading`, `command` ; `output` si mesuré | `syntax`, `phase` | La machine, la commande dans un bloc de code, sa sortie réelle ou `not_captured`, sous `heading`. `phase` nomme le rôle quand `target_role` est absent. |
| `server`, `variable` | `placeholder` (`<UPPER_SNAKE>`), `meaning`, `value`, `heading` | | Une ligne du tableau sous `heading`, identifiée par son placeholder. |
| `contact` | `name`, `role`, `channel`, `heading` | | Un élément de liste sous `heading`. |
| `decision` | `decision`, `reason` | `discarded` | Un élément de liste daté sous le libellé `decisions`. |
| `fact` | `heading`, `fact` | | Un encadré d'observation daté (`> **On Mets, <date>.**`) sous `heading`. |
| `open_question` | `question`, `owner` | | Un élément de liste avec son responsable sous le libellé `open_questions`. |

### Routage

Chaque étape refuse plutôt que de deviner, et le refus liste les candidats :

1. **Runbook.** Le sujet désigne un seul dossier de runbook. Un mot inconnu ne crée
   jamais de runbook ; un sujet enregistré ou un chemin de dossier le peut, sous
   l'unique racine, ou sous la racine que son chemin désigne.
2. **Rôle.** `target_role`, sinon `phase` pour une commande, sinon le routage du
   gabarit.
3. **Fichier.** Le seul fichier dont le nom répond au motif du rôle. Aucun : le
   `stem` du rôle est créé, si le rôle a un `title`. Plusieurs : refus.
4. **Section.** Le H2 que désigne le type. Il est retrouvé sans tenir compte de la
   casse ni des espaces, et créé en fin de fichier s'il manque.

### Ce qu'une capture fait à un fichier

- **Provenance** : une marque neutre par entrée, `<measured|said|unverified> AAAA-MM-JJ`,
  qui ne nomme ni outil, ni assistant, ni session.
- **Déjà présent** (le même texte, quelle que soit la date de provenance) : rien n'est
  écrit, le reçu dit `duplicate_skipped`.
- **Un symptôme connu avec un contenu nouveau** reçoit un paragraphe daté `Update`
  dans sa section ; l'entrée existante reste telle quelle.
- **Une ligne de tableau** de même placeholder est remplacée sur place ; les octets
  précédents restent dans l'historique de la note.
- **Tout le reste** est ajouté. Un encadré d'observation porte sa date : le même fait
  observé plus tard donne un nouvel encadré, jamais une modification de l'ancien.
- **Frontmatter** : un fichier qui n'en a pas n'en reçoit pas. Un fichier qui en a un
  reçoit `last_verified` seulement d'une capture `measured`, et `updated` seulement s'il
  a déjà cette clé. `origin` et `confidence` ne sont jamais écrits.
- **Secrets** : un champ qui contient ce qui ressemble à un secret (le détecteur de
  masquage et `DATACRON_SECRET_REDACTION_PATTERNS`) est refusé, sans être répété. Il est
  refusé plutôt que masqué : une commande masquée est une commande fausse. Une empreinte
  écrite `thumbprint=...` est refusée aussi ; l'écrire avec un placeholder.
- **Structure** : une prose que Markdown lirait comme un titre est refusée, et chaque
  écriture est vérifiée pour laisser lisibles tous les autres titres du fichier.

Le reçu donne `runbook`, `rel_path`, `role`, `heading`, `action` (`created`,
`appended`, `updated`, `duplicate_skipped`), `language`, `runbook_created`, `written`,
et pour une écriture `content_hash`, `operation_id` et `indexed`.

### Comment le réflexe atteint le modèle

La capture doit se faire au fil du travail, pas sur demande. Trois signaux côté
serveur portent ce réflexe, et aucun ne coûte un fichier de règles client :

- **Instructions serveur.** Quand le serveur propose `runbook_capture`, ses
  instructions sont le bloc du protocole mémoire suivi d'un paragraphe sur les runbooks,
  et rien d'autre. Sans l'outil, elles sont exactement le bloc du protocole. Le
  paragraphe ne fait pas partie du contrat mémoire : sa version et son hash ne changent
  pas, et `datacron protocol install` ne l'écrit jamais dans les règles d'un client,
  dont le budget (6000 caractères pour Windsurf) est déjà presque rempli par le bloc.
- **Description de l'outil.** La description de `runbook_capture` rappelle quand
  l'appeler, les trois valeurs de provenance et le refus des suppositions et des
  secrets, pour les clients qui ignorent les instructions serveur.
- **`session_context`.** Avec un `subject` qui désigne un seul runbook existant, le
  résultat porte `runbook: {path, language}`. Il est ajouté après le placement des
  sources et retiré s'il ne tient pas : il ne coûte jamais une source.

### Défaire une capture

Chaque écriture est journalisée : `get_note_history` et `audit_query` la listent.
`revert_note` n'atteint pas les runbooks, parce qu'il est lié à `DATACRON_WRITE_PATHS` ;
quand un runbook est un arbre de travail git, `git diff` montre la capture et `git restore`
la défait.

## Frontière d'écriture

Déclarer une racine ne la rend pas inscriptible. Les écritures de runbook ont leur
propre réglage :

| Variable | Défaut | Rôle |
|---|---|---|
| `DATACRON_RUNBOOK_WRITE_PATHS` | vide | Allowlist d'écriture des runbooks, séparée par le séparateur de chemins de l'OS. **Vide = aucun runbook inscriptible.** Seule la capture de runbook la lit ; les outils d'écriture génériques gardent `DATACRON_WRITE_PATHS` et ne lisent jamais celle-ci. |

Un fichier de runbook n'est inscriptible que si toutes ces conditions tiennent,
jugées après résolution du chemin (un segment `..` est suivi d'abord) :

1. il est dans `DATACRON_RUNBOOK_WRITE_PATHS` et dans le vault ;
2. il est dans un dossier d'une racine déclarée, nommé selon `directory_naming` ;
3. c'est un fichier Markdown ;
4. il n'est pas sous `knowledge/` à la racine du vault ;
5. aucun dossier de son chemin n'est `.git`, `confluence-source` ou
   `confluence-updates`.

Les règles 4 et 5 sont fixes : un vault ne peut pas les lever, et elles se comparent
sans tenir compte de la casse sur tout système de fichiers. Ce sont les seuls noms de
dossier que Datacron connaît d'avance ; tous les autres viennent du vault. L'écriture
passe ensuite par l'écrivain ordinaire, avec son verrou, son journal, son admission des
notes et son refus d'un chemin qui traverse un lien symbolique ou une jonction.

Datacron ne lance jamais git dans un runbook : il écrit des fichiers, et la relecture
du diff comme le commit restent manuels.
