# Runbooks

**Français** | [English](../en/runbooks.md)

Un runbook, c'est un dossier par sujet, un fichier Markdown par thème numéroté
(`00-architecture`, `50-troubleshooting`, `90-variables`, etc.) et un dossier
`quickref/` de fiches de survie par service. Un vault peut déclarer où vivent ses
runbooks et à quoi ils ressemblent, pour que Datacron range un fait durable dans le
bon runbook et la bonne section.

Cette page décrit ce qui est disponible aujourd'hui : le bloc `runbooks` de
`.datacron/VAULT.yaml`, le réglage d'écriture dédié et la frontière d'écriture qu'ils
définissent. Aucun outil n'écrit encore dans un runbook.

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
      skeleton_line: "State: skeleton. Nothing in this file is measured yet."
      roles:
        architecture:    {stem: 00-architecture,    match: '^\d{2}-architecture$'}
        troubleshooting: {stem: 50-troubleshooting, match: '^\d{2}-troubleshooting$', entry: symptom}
        escalation:      {stem: 60-escalation,      match: '^\d{2}-.*escalation$',   entry: contact}
        variables:       {stem: 90-variables,       match: '^\d{2}-variables$',      entry: table_row}
        measured:        {stem: MEASURED,           match: '^MEASURED$', optional: true}
      quickref: {folder: quickref, language: fr}
      labels: {symptom: Symptom, cause: Cause, fix: Fix}
      routing:
        error_fix: troubleshooting
        contact: escalation
        server: variables
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
| `language` | Langue des fichiers numérotés (`en`, `fr`). |
| `readme` | Nom du fichier d'index, sans `.md`. |
| `roles` | Une entrée par rôle de fichier (identifiant en minuscules). |
| `quickref` | Facultatif : `folder`, sa propre `language`, et `naming` avec `{service}` une seule fois (par défaut `{service}`). |
| `observation_label` | Libellé des encadrés d'observation datés, par exemple `On Mets`. |
| `labels` | Chaque mot que Datacron écrira, dans la langue du gabarit. Datacron n'embarque aucun texte de runbook. |
| `skeleton_line` | La ligne unique que porte un fichier vide. |
| `routing` | Type de capture vers rôle : `error_fix`, `command`, `server`, `variable`, `contact`, `decision`, `fact`, `open_question`. Un type absent n'a pas de rôle par défaut. |

### Rôles

Un rôle se trouve par le motif auquel répond le nom de son fichier, pas par son
numéro : le runbook WSUS range le troubleshooting dans `50-troubleshooting`, le
runbook AD dans `70-troubleshooting`, et les deux répondent à
`^\d{2}-troubleshooting$`.

| Clé | Sens |
|---|---|
| `stem` | Nom de fichier qu'utilise une création, sans `.md`. Il doit répondre à son propre `match`, et au motif d'aucun autre rôle, sinon la capture suivante ne retrouverait pas le fichier. |
| `match` | Expression régulière appliquée au nom entier. Un motif à répétition imbriquée est refusé. |
| `entry` | Forme d'entrée facultative : `symptom`, `contact` ou `table_row`. |
| `optional` | `true` quand le fichier peut manquer, `false` par défaut. |

## Frontière d'écriture

Déclarer une racine ne la rend pas inscriptible. Les écritures de runbook ont leur
propre réglage :

| Variable | Défaut | Rôle |
|---|---|---|
| `DATACRON_RUNBOOK_WRITE_PATHS` | vide | Allowlist d'écriture des runbooks, séparée par le séparateur de chemins de l'OS. **Vide = aucun runbook inscriptible.** Seule la capture de runbook la lit ; les outils d'écriture génériques gardent `DATACRON_WRITE_PATHS` et ne lisent jamais celle-ci. |

Un fichier de runbook n'est inscriptible que si toutes ces conditions tiennent,
jugées après résolution du chemin (un segment `..` ou un lien est suivi d'abord) :

1. il est dans `DATACRON_RUNBOOK_WRITE_PATHS` et dans le vault ;
2. il est dans un dossier d'une racine déclarée, nommé selon `directory_naming` ;
3. c'est un fichier Markdown ;
4. il n'est pas sous `knowledge/` à la racine du vault ;
5. aucun dossier de son chemin n'est `.git`, `confluence-source` ou
   `confluence-updates`.

Les règles 4 et 5 sont fixes : un vault ne peut pas les lever, et elles se comparent
sans tenir compte de la casse sur tout système de fichiers. Ce sont les seuls noms de
dossier que Datacron connaît d'avance ; tous les autres viennent du vault.

Datacron ne lance jamais git dans un runbook : il écrit des fichiers, et la relecture
du diff comme le commit restent manuels.
