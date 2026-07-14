# Guide d'installation et de configuration

**Français** · [English](../en/setup.md)

Ce guide t'amène d'un dossier de notes Markdown à un serveur Datacron opérationnel,
branché sur Claude Desktop ou Claude Code. Il complète le [README](../../README.md) et le
[guide utilisateur](user-guide.md).

> Datacron ne modifie jamais tes notes sans que tu l'actives explicitement, et n'envoie
> rien vers un service cloud. Il ajoute seulement un dossier `.datacron/` à côté de tes notes.

## Parcours guidé (le plus simple)

Une seule commande fait tout — initialisation du sidecar, construction de l'index et
branchement du client MCP :

```bash
datacron setup
```

Elle pose des questions avec des valeurs par défaut (emplacement du vault, client, activation
de l'écriture, durabilité, lecture seule), puis exécute `init`, indexe et écrit la config du
client, avant d'afficher un récapitulatif et comment vérifier. Options utiles :

- `datacron setup --yes` — accepte tous les défauts, sans question (installation automatique).
- `datacron setup --vault CHEMIN --client claude-desktop` — cible un vault précis.
- `datacron setup --enable-write --write-path CHEMIN` — active l'écriture sur un sous-dossier (défaut : `<vault>/_memory`).
- `datacron setup --durability strict --read-only` — mode durabilité strict et lecture seule certifiée.
- `datacron setup --no-index` — saute la construction de l'index.
- `datacron setup --client claude-code` — affiche un snippet de config stdio prêt à coller dans Claude Code.
- `datacron setup --client none` — configure le vault sans écrire ni afficher de config client.

Les sections ci-dessous décrivent les **mêmes étapes manuellement**, si tu préfères tout
contrôler pas à pas.

## 1. Prérequis

Avant de commencer, vérifie que tu disposes de :

| Prérequis | Détail |
|---|---|
| Python 3.11+ | `python --version` doit renvoyer 3.11 ou plus. |
| `ripgrep` | Binaire `rg` accessible dans le `PATH`. Nécessaire pour `search_regex`. |
| Un vault Markdown | N'importe quel dossier de fichiers `.md`. Il reste un dossier normal. |
| Un client MCP | Claude Desktop (installateur automatique) ou tout client MCP stdio. |

Vérification rapide de `ripgrep` :

```bash
rg --version
```

Si `rg` n'est pas trouvé, installe-le (`winget install BurntSushi.ripgrep.MSVC` sous
Windows, `apt install ripgrep` / `brew install ripgrep` ailleurs) ou pointe Datacron vers
un binaire précis avec la variable `DATACRON_RIPGREP_PATH`.

## 2. Installation

Depuis un clone du dépôt :

```bash
python -m pip install -e ".[dev]"
```

Pour installer uniquement l'application, sans les outils de développement :

```bash
python -m pip install -e .
```

L'installation expose deux commandes :

- `datacron` — la CLI (init, index, statut, éval, gestion du serveur MCP).
- `datacron-mcp` — l'entrée directe du serveur stdio, utilisée par l'installateur.

Vérifie que la CLI répond :

```bash
datacron --help
```

## 3. Initialiser le vault

`datacron init` crée le sidecar `.datacron/` (index, logs, historique, journal
d'opérations) et un fichier de configuration `VAULT.yaml`.

```bash
datacron init /chemin/vers/vault
```

Sortie attendue :

```text
Initialized Datacron vault at /chemin/vers/vault
  sidecar:    /chemin/vers/vault/.datacron
  config:     /chemin/vers/vault/.datacron/VAULT.yaml
  vault_id:   01J...
```

Si un `VAULT.yaml` existe déjà, `init` le laisse intact ; utilise `--force` pour le
réécrire. La commande crée le dossier du vault s'il n'existe pas encore.

## 4. Construire l'index

L'index FTS5 est ce qui permet la recherche BM25. Construis-le une première fois :

```bash
datacron index --vault /chemin/vers/vault
```

- `datacron index` est **incrémental** : il ignore les notes inchangées (gate sur mtime),
  re-découpe les notes modifiées et supprime les notes disparues.
- `datacron reindex` reconstruit un index complet dans une base séparée, la valide
  (hash + SQLite), puis la bascule atomiquement sur l'index vivant. À utiliser si l'index
  paraît incohérent.

À noter : le serveur MCP répare aussi l'index à la lecture, donc un `index` manuel n'est
strictement nécessaire que pour le premier peuplement ou après un gros changement hors ligne.

## 5. Vérifier l'état

```bash
datacron status --vault /chemin/vers/vault
```

Sortie type :

```text
Datacron 0.1.0.dev0
  vault_root: /chemin/vers/vault
  initialized: yes
  vault_id:   01J...
  created:    2026-07-14T...
  notes:      312
  index:      built (312 notes, 1450 chunks)
  log file:   /chemin/vers/vault/.datacron/logs/datacron-20260714.log
```

Si `initialized: no` apparaît, relance `datacron init`. Si `index: not built` ou `empty`,
relance `datacron index`.

## 6. Brancher un client MCP

### Claude Desktop (installateur automatique)

```bash
datacron mcp install --client claude-desktop --vault /chemin/vers/vault
```

La commande écrit l'entrée serveur dans la configuration de Claude Desktop et fixe
l'allowlist de lecture sur le vault. **Redémarre Claude Desktop** pour que le changement
prenne effet. Seul `claude-desktop` est supporté par l'installateur automatique aujourd'hui.

Pour cibler un fichier de configuration précis (test, install non standard) :

```bash
datacron mcp install --client claude-desktop --vault /chemin/vers/vault --config-path /chemin/config.json
```

### Claude Code ou autre client stdio

Les clients qui savent lancer un serveur MCP stdio peuvent utiliser directement l'une des
deux entrées :

```bash
datacron mcp serve --vault /chemin/vers/vault
```

ou l'entrée script, qui lit le vault depuis `DATACRON_VAULT_ROOT` :

```bash
DATACRON_VAULT_ROOT=/chemin/vers/vault datacron-mcp
```

Déclare cette commande dans la configuration MCP du client. Le serveur lit les messages
JSON-RPC sur stdin et répond sur stdout ; les logs partent dans le FileLogger, jamais sur
stdout (réservé au protocole).

## 7. Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `DATACRON_VAULT_ROOT` | `--vault` ou répertoire courant | Vault servi par le serveur. |
| `DATACRON_READ_PATHS` | vide | Allowlist de lecture ; l'installateur la fixe au vault. |
| `DATACRON_WRITE_PATHS` | vide | Allowlist d'écriture ; **vide = écriture désactivée**. |
| `DATACRON_MAX_RESULT_COUNT` | `20` | Nombre max de résultats retournés. |
| `DATACRON_MAX_RESULT_TOKENS` | `8000` | Budget token des résultats de recherche. |
| `DATACRON_GET_NOTE_MAX_TOKENS` | `25000` | Budget de `get_note(format="full")`. |
| `DATACRON_CHUNK_MAX_TOKENS` | `1024` | Taille cible max des chunks. |
| `DATACRON_RIPGREP_PATH` | `rg` | Binaire ripgrep. |

Les listes de chemins utilisent le séparateur de l'OS : `:` sous Unix, `;` sous Windows.

## 8. Activer l'écriture (optionnel)

Les outils d'écriture sont **désactivés par défaut**. Sans `DATACRON_WRITE_PATHS`, ils
renvoient une erreur claire et ne créent aucun fichier. Pour autoriser l'écriture sur un
sous-dossier précis :

```powershell
$env:DATACRON_VAULT_ROOT = "G:\_DATA"
$env:DATACRON_READ_PATHS = "G:\_DATA"
$env:DATACRON_WRITE_PATHS = "G:\_DATA\_memory"
datacron mcp serve --vault G:\_DATA
```

L'écriture reste confinée à `DATACRON_WRITE_PATHS`, atomique (fichier temporaire +
`os.replace`), historisée par contenu avant modification, et auditée. Garde une règle
**single-writer** : l'écriture concurrente multi-machines n'est pas supportée.

Détails et garanties : [Frontière de sécurité](security-boundary.md).

## 9. Intégrité (optionnel)

Pour surveiller la corruption silencieuse de l'index et des notes, initialise les
sentinelles d'intégrité puis lance une passe de scrub :

```bash
datacron scrub-init --vault /chemin/vers/vault
datacron scrub --vault /chemin/vers/vault
```

`scrub` est résumable et en mode alerte seule ; il sort en code 2 si des anomalies sont
détectées. Voir [Scrubber d'intégrité](integrity-scrubber.md) et
[Santé opérationnelle](operational-health.md).

## 10. Vérification finale

Depuis ton client MCP (Claude), demande un appel à `get_health` : il renvoie l'état réel
de fraîcheur de l'index, d'intégrité, de checksum, de durabilité et des invariants. Si tout
est vert et que `list_notes` renvoie tes notes, l'installation est opérationnelle.

Pour la suite, passe au [guide utilisateur](user-guide.md).

## Construire l'exécutable autonome (optionnel)

Pour distribuer Datacron à des utilisateurs sans Python, un exécutable autonome (un seul
fichier, ~22 Mo) se construit avec PyInstaller :

```powershell
# Windows
pip install -e ".[build]"
./scripts/build_installer.ps1        # produit dist/datacron.exe
```

```bash
# Linux / macOS
pip install -e ".[build]"
./scripts/build_installer.sh         # produit dist/datacron
```

Le binaire embarque la CLI complète (dont `datacron setup`) et ses données packagées ; il ne
requiert aucun Python installé. `dist/` et `build/` ne sont pas versionnés. Contexte : ADR-017
dans [l'architecture](architecture.md).

## Dépannage

| Symptôme | Cause probable | Correctif |
|---|---|---|
| `No vault root provided` | Ni `--vault`, ni `DATACRON_VAULT_ROOT`, ni `.datacron/VAULT.yaml` dans le dossier courant. | Passe `--vault` ou définis `DATACRON_VAULT_ROOT`. |
| `search_regex` échoue | `ripgrep` introuvable. | Installe `rg` ou définis `DATACRON_RIPGREP_PATH`. |
| Les write tools renvoient une erreur | `DATACRON_WRITE_PATHS` vide (comportement normal par défaut). | Définis l'allowlist d'écriture (section 8). |
| `index: not built` dans `status` | Index jamais construit. | `datacron index --vault ...`. |
| Claude Desktop ne voit pas Datacron | Client non redémarré après `mcp install`. | Redémarre Claude Desktop. |
