# Runbooks

**Français** | [English](../en/runbooks.md)

Un runbook, c'est un dossier par projet, un fichier Markdown par thème numéroté
(`00-architecture`, `50-troubleshooting`, `90-variables`, etc.) et un dossier
`quickref/` de fiches de survie par service. Datacron range les runbooks dans un
dossier de votre vault, le vault runbooks. Quand une conversation porte sur un
projet, `runbook_capture` range chaque fait durable dans le runbook de ce projet et
dans la bonne section, dès que le travail l'établit, et crée le runbook depuis votre
gabarit quand le projet n'en a pas encore.

## Configurer le vault runbooks

Choisissez le vault runbooks lors de la configuration de Datacron :

```powershell
datacron setup --vault C:\Notes --runbook-vault C:\Notes\Runbooks
```

`setup` écrit `DATACRON_RUNBOOK_VAULT` dans l'entrée du client ; une nouvelle
exécution sans l'option le conserve. Vous pouvez aussi définir la variable vous-même :

| Variable | Défaut | Rôle |
|---|---|---|
| `DATACRON_RUNBOOK_VAULT` | non définie | Le dossier du vault qui contient les runbooks, un sous-dossier par projet. **Non définie = aucun runbook inscriptible**, et `runbook_capture` n'est pas proposé. |

Le vault runbooks doit se trouver dans le vault servi : ses fichiers partagent les
chemins, les verrous et le journal d'opérations du vault, si bien qu'une reprise après
incident les retrouve là où ils ont été écrits. Un vault runbooks hors du vault est
refusé par `setup`, et le serveur journalise alors un avertissement et ne propose pas
la capture. Le nom du dossier vous appartient ; Datacron n'en connaît aucun d'avance.

## Le bloc `runbooks`

Le bloc de `.datacron/VAULT.yaml` dit à quoi ressemble un runbook. Il est facultatif :
un vault qui n'en a pas se comporte exactement comme avant, et la capture n'est pas
proposée. Les clés inconnues sont refusées au démarrage : une clé mal orthographiée
échoue bruyamment au lieu d'être ignorée.

```yaml
runbooks:
  directory_naming: "{slug}-runbook"   # valeur par défaut
  default_template: infra              # requis avec plusieurs gabarits
  assignments: {bench: lab}            # facultatif : slug du runbook -> gabarit
  protected_paths: [shared/archive]    # relatifs au vault runbooks ; vide par défaut
  templates:
    infra:
      language: en
      readme: README
      observation_label: On site
      roles:
        architecture:    {stem: 00-architecture,    match: '^\d{2}-architecture$', title: Architecture}
        troubleshooting: {stem: 50-troubleshooting, match: '^\d{2}-troubleshooting$', title: Troubleshooting}
        escalation:      {stem: 60-escalation,      match: '^\d{2}-.*escalation$', title: Escalation}
        variables:       {stem: 90-variables,       match: '^\d{2}-variables$', title: Variables}
      quickref: {folder: quickref, language: fr}
      labels:
        readme_title: "{slug} runbook"
        documents: Documents
        skeleton: "Not captured yet."
        symptom: Symptom
        cause: Cause
        fix: Fix
        not_captured: Not captured
        update: Update
        machine: Machine
        output: Output
        placeholder: Placeholder
        meaning: Meaning
        value: Value
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

| Clé | Sens |
|---|---|
| `directory_naming` | Nom d'un dossier de runbook dans le vault runbooks. Il contient `{slug}` une seule fois plus une partie fixe, pour que tout dossier de cet endroit ne soit pas un runbook. Par défaut `{slug}-runbook`. |
| `templates` | Un ou plusieurs gabarits (ci-dessous). |
| `default_template` | Le gabarit d'un runbook sans affectation. Requis quand plusieurs gabarits sont déclarés. |
| `assignments` | Table facultative du slug d'un runbook vers le gabarit qu'il utilise, pour les runbooks qui ne suivent pas le gabarit par défaut. |
| `protected_paths` | Chemins, relatifs au vault runbooks, que l'écrivain de runbooks refuse toujours, comparés sans tenir compte de la casse. Vide par défaut. |

### Gabarits

| Clé | Sens |
|---|---|
| `language` | Langue des fichiers numérotés (`en`, `fr`). Chaque reçu la rappelle, pour que l'appelant écrive l'entrée dans cette langue. |
| `readme` | Nom du fichier d'index, sans `.md`. Aucun rôle ne peut l'utiliser. |
| `roles` | Une entrée par rôle de fichier (identifiant en minuscules). |
| `quickref` | Facultatif : `folder`, sa propre `language`, et `naming` avec `{service}` une seule fois (par défaut `{service}`). Le dossier est réservé ; la capture n'écrit pas encore de fiche de survie. |
| `observation_label` | Libellé des encadrés d'observation datés, par exemple `On site`. |
| `labels` | Chaque mot que Datacron écrit, dans la langue du gabarit. Une capture dont le type demande un libellé que le gabarit ne déclare pas est refusée, en le nommant. Créer un runbook demande `readme_title` (où `{slug}` est remplacé), `documents` et `skeleton`. Datacron n'embarque aucun texte de runbook. |
| `routing` | Type de capture vers rôle : `error_fix`, `command`, `server`, `variable`, `contact`, `decision`, `fact`, `open_question`. Un type absent n'a pas de rôle par défaut, et une capture de ce type doit en nommer un. |

### Rôles

Un rôle se trouve par le motif auquel répond le nom de son fichier, pas par son
numéro : un runbook range le troubleshooting dans `50-troubleshooting`, un
autre dans `70-troubleshooting`, et les deux répondent à
`^\d{2}-troubleshooting$`.

| Clé | Sens |
|---|---|
| `stem` | Nom de fichier qu'utilise une création, sans `.md`. Il doit répondre à son propre `match`, et au motif d'aucun autre rôle, sinon la capture suivante ne retrouverait pas le fichier. |
| `match` | Expression régulière appliquée au nom entier. Un motif à répétition imbriquée est refusé. |
| `title` | Le titre `# ` d'un fichier que ce rôle crée. Un rôle sans titre n'est jamais créé ; on n'écrit dedans que si son fichier existe. |

## Capturer un fait : `runbook_capture`

L'outil n'est enregistré que si le vault déclare `runbooks`, que
`DATACRON_RUNBOOK_VAULT` désigne un dossier du vault, que le serveur n'est pas en
lecture seule et que le profil d'outils est `standard`. Sinon, la liste des outils ne
change pas.

| Argument | Sens |
|---|---|
| `subject` | Le projet dont parle la conversation : son nom, un sujet enregistré (tag, dernier segment ou alias), ou le chemin d'un dossier de runbook comme `Runbooks/mail-runbook`. |
| `kind` | L'un des huit types ci-dessous. |
| `evidence` | `measured` (une commande réellement passée, sa sortie réelle), `said` (une réunion datée ou une personne nommée) ou `unverified`. |
| `source` | Une ligne qui dit d'où vient le fait. Elle est gardée dans le journal d'opérations, pas dans le runbook. |
| `fields` | Les champs du type, ci-dessous. Un champ inconnu est refusé. |
| `observed_on` | Date de l'événement, `AAAA-MM-JJ`, jamais dans le futur. Par défaut : aujourd'hui (UTC). |
| `target_role` | Un rôle qui remplace le routage. |
| `create_runbook` | `true` crée le runbook quand le projet n'en a pas encore (ci-dessous). Par défaut `false`. |
| `dry_run` | `true` renvoie le plan sans rien écrire. |
| `request_id` | Rend une reprise sûre : une reprise identique rejoue le premier reçu. |

| Type | Champs requis | Champs facultatifs | Écrit sous la forme |
|---|---|---|---|
| `error_fix` | `symptom`, `fix` | `cause` | Un H2 `Symptom: <symptom>` avec `Cause`, `Fix` et la provenance. Une cause absente s'écrit avec le libellé `not_captured`. |
| `command` | `machine`, `heading`, `command` ; `output` si mesuré | `syntax`, `phase` | La machine, la commande dans un bloc de code, sa sortie réelle ou `not_captured`, sous `heading`. `phase` nomme le rôle quand `target_role` est absent. |
| `server`, `variable` | `placeholder` (`<UPPER_SNAKE>`), `meaning`, `value`, `heading` | | Une ligne du tableau sous `heading`, identifiée par son placeholder. |
| `contact` | `name`, `role`, `channel`, `heading` | | Un élément de liste sous `heading`. |
| `decision` | `decision`, `reason` | `discarded` | Un élément de liste daté sous le libellé `decisions`. |
| `fact` | `heading`, `fact` | | Un encadré d'observation daté (`> **On site, <date>.**`) sous `heading`. |
| `open_question` | `question`, `owner` | | Un élément de liste avec son responsable sous le libellé `open_questions`. |

### Créer un runbook

Le nom d'un runbook vient du projet dont parle la conversation. Un sujet qui ne
désigne aucun runbook existant est refusé, et le refus dit de recommencer avec
`create_runbook=true` ; c'est cette demande explicite qui empêche une faute de frappe
de démarrer un runbook. Avec elle, la capture crée `<slug>-runbook/` dans le vault
runbooks, le slug étant tiré du nom du projet (minuscules, chiffres et tirets) ou du
sujet enregistré, et écrit :

1. le fichier capturé, avec le titre de son rôle et l'entrée ;
2. un fichier par autre rôle qui a un titre, avec seulement ce titre et le libellé
   `skeleton` ;
3. le README, titré avec `readme_title`, qui liste tous les fichiers sous le libellé
   `documents`.

Le fait capturé est validé en premier. Un fichier du squelette qui ne peut pas être
écrit est absent de la liste `scaffold` du reçu, et la capture suivante sur son rôle le
crée. `dry_run` montre tout le plan, `scaffold` compris, sans rien écrire.

### Routage

Chaque étape refuse plutôt que de deviner, et le refus liste les candidats :

1. **Runbook.** Le sujet désigne un dossier de runbook du vault runbooks, ou un
   nouveau avec `create_runbook=true`.
2. **Rôle.** `target_role`, sinon `phase` pour une commande, sinon le routage du
   gabarit.
3. **Fichier.** Le seul fichier dont le nom répond au motif du rôle. Aucun : le
   `stem` du rôle est créé, si le rôle a un `title`. Plusieurs : refus.
4. **Section.** Le H2 que désigne le type. Il est retrouvé sans tenir compte de la
   casse ni des espaces, et créé en fin de fichier s'il manque.

### Ce qu'une capture fait à un fichier

- **Provenance** : une marque neutre par entrée, `<measured|said|unverified> AAAA-MM-JJ`,
  qui ne nomme ni outil, ni assistant, ni session.
- **Déjà présent** (le même texte dans la même section, quelle que soit la date de
  provenance) : rien n'est écrit, le reçu dit `duplicate_skipped`. Un texte présent
  dans une autre section du fichier n'est pas détecté.
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
`appended`, `updated`, `duplicate_skipped`), `language`, `runbook_created`, `scaffold`,
`written`, et pour une écriture `content_hash`, `operation_id` et `indexed`.

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
  l'appeler, comment un projet obtient son runbook, les trois valeurs de provenance et
  le refus des suppositions et des secrets, pour les clients qui ignorent les
  instructions serveur.
- **`session_context`.** Avec un `subject` qui désigne un seul runbook existant, le
  résultat porte `runbook: {path, language}`. Il est ajouté après le placement des
  sources et retiré s'il ne tient pas : il ne coûte jamais une source.

### Défaire une capture

Chaque écriture est journalisée : `get_note_history` et `audit_query` la listent.
`revert_note` n'atteint pas les runbooks, parce qu'il est lié à `DATACRON_WRITE_PATHS` ;
quand un runbook est un arbre de travail git, `git diff` montre la capture et `git restore`
la défait.

## Frontière d'écriture

Un fichier de runbook n'est inscriptible que si toutes ces conditions tiennent,
jugées après résolution du chemin (un segment `..` est suivi d'abord) :

1. il est dans `DATACRON_RUNBOOK_VAULT`, lui-même dans le vault ;
2. son premier dossier sous le vault runbooks est nommé selon `directory_naming` ;
3. c'est un fichier Markdown ;
4. il n'est sous aucun des `protected_paths` ;
5. aucun dossier de son chemin n'est `.git`, `confluence-source` ou
   `confluence-updates`.

La règle 5 est fixe : un vault ne peut pas la lever. Ces trois noms génériques sont les
seuls noms de dossier que Datacron connaît d'avance ; tous les autres, chemins protégés
compris, viennent de votre configuration. Les règles 4 et 5 se comparent sans tenir
compte de la casse sur tout système de fichiers. L'écriture passe ensuite par
l'écrivain ordinaire, avec son verrou, son journal, son admission des notes et son
refus d'un chemin qui traverse un lien symbolique ou une jonction. Les outils
d'écriture génériques ne traitent jamais le vault runbooks comme un chemin
d'écriture.

Datacron ne lance jamais git dans un runbook : il écrit des fichiers, et la relecture
du diff comme le commit restent manuels.
