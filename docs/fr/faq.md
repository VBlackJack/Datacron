# Questions fréquentes

**Français** | [English](../en/faq.md)

Ces réponses décrivent le comportement actuel de Datacron. Pour les procédures complètes, voir le
[guide d'installation et de configuration](setup.md), le
[guide de l'installeur Windows](installation-windows.md) et la
[santé opérationnelle et durabilité](operational-health.md).

## Datacron a configuré le mauvais dossier - ou mon profil utilisateur - comme vault. Pourquoi et comment le réparer ?

Le setup interactif utilise le chemin de vault sélectionné ; son défaut est le dossier courant.
Les versions actuelles expliquent ce choix avant de poser la question. Le mode non interactif
`setup --yes` n'adopte plus un dossier arbitraire : il exige `--vault`,
`DATACRON_VAULT_ROOT` ou un `.datacron/VAULT.yaml` existant dans le dossier courant. La racine du
profil utilisateur est toujours refusée, même si elle est passée explicitement. Un sous-dossier
dédié dans le profil reste valide.

Retire uniquement les entrées MCP de Datacron pour la mauvaise cible, puis configure le bon
dossier :

```bash
datacron unregister --client all --scope both --vault "MAUVAIS_CHEMIN"
datacron setup --client all --scope both --vault "BON_CHEMIN"
```

`unregister` préserve les autres serveurs MCP et ne supprime aucune note. Si tu as aussi installé
un protocole mémoire au scope projet, retire séparément son bloc marqué depuis le workspace où il
a été installé :

```bash
datacron protocol uninstall --client all --scope project --project "CHEMIN_WORKSPACE"
```

N'utilise pas `--reset` pour déplacer un vault : reset agit seulement sur la configuration et
l'index Datacron du vault sélectionné, pas sur les enregistrements clients.

## Pourquoi mon client IA ne peut-il pas écrire de notes ?

Les outils d'écriture sont opt-in. Sans `--enable-write`, le client MCP ne reçoit aucune
allowlist d'écriture et Datacron n'expose pas d'accès effectif en écriture. Avec l'opt-in et sans
`--write-path` explicite, le setup confine les écritures à `<vault>/_memory`,
`<vault>/_drafts` et `<vault>/_journal`. Les chemins hors allowlist restent en lecture seule. Le
mode certifié `--read-only` bloque les écritures même si des chemins sont autorisés.

Active l'écriture après l'installation en relançant le setup, puis redémarre le client IA pour
qu'il recharge la configuration MCP :

```bash
datacron setup --yes --vault "CHEMIN_VAULT" --client all --scope both --enable-write
```

Utilise `--write-path` pour choisir une frontière explicite ; le prompt interactif des répertoires
accepte aussi une liste séparée par le séparateur de chemins du système. Ajoute
`--machine-wide-write` seulement si les futurs clients doivent hériter de l'allowlist via
l'environnement utilisateur ; c'est un opt-in séparé qui ne rend pas inscriptibles les chemins
hors allowlist.

## Pourquoi Antigravity ne voit-il pas Datacron ?

Datacron détecte le profil Antigravity actif uniquement dans `~/.gemini/antigravity`. Au scope
utilisateur, il fusionne le serveur `datacron` dans `~/.gemini/config/mcp_config.json`. Au scope
projet, il écrit `<vault>/.agents/mcp_config.json` ; Antigravity ne charge ce fichier que lorsque
le dossier du vault est ouvert comme workspace dans l'IDE. Redémarre Antigravity après le setup
pour qu'il recharge la configuration MCP.

```bash
datacron setup --yes --vault "CHEMIN_VAULT" --client antigravity --scope both
```

Le chemin workspace a été validé E2E le 2026-07-19. Datacron écrit et teste unitairement la cible
utilisateur, mais la découverte de ce fichier global par Antigravity doit encore être validée avec
la version installée de l'IDE. Si la route globale ne se charge pas, ouvre le vault comme
workspace et utilise la route `.agents/mcp_config.json` validée. Les anciens dossiers de profil
`antigravity-ide` et `antigravity-backup` sont volontairement ignorés.

## Pourquoi LM Studio ne voit-il pas Datacron ?

LM Studio 0.3.17+ possède une seule configuration MCP utilisateur dans
`~/.lmstudio/mcp.json` et aucune configuration projet. Datacron détecte le vrai dossier de
profil `~/.lmstudio`, puis fusionne uniquement l'entrée `datacron` et préserve tous les autres
serveurs et réglages.

```bash
datacron setup --yes --vault "CHEMIN_VAULT" --client lmstudio --scope user
```

Redémarre LM Studio après le setup pour qu'il recharge le fichier. N'utilise pas
`--scope project` : LM Studio ne possède aucune cible projet. Son absence de
`datacron protocol install` est aussi volontaire, car la documentation officielle ne définit
aucun fichier d'instructions globales. Le [deeplink du README](../../README.md#add-to-lm-studio)
est une alternative manuelle pour les installations Python, mais ses placeholders
`<YOUR_VAULT>` doivent être remplacés dans l'éditeur MCP.

## Pourquoi la CLI répond-elle « Unknown client X » alors que la documentation le mentionne ?

La documentation peut provenir d'une révision du dépôt plus récente que l'exécutable présent sur
ton `PATH`. Vérifie à la fois la version et l'exécutable lancé :

```bash
datacron --version
```

Sous Windows, `where datacron` ou `Get-Command datacron` identifie l'exécutable. Réinstalle ou
mets à niveau la release Datacron courante - avec le dernier `Datacron-Setup.exe` ou
`python -m pip install --upgrade datacron` - puis ouvre un nouveau terminal et redémarre le client
IA. Lancer un ancien binaire installé depuis un checkout source récent n'ajoute pas les nouveaux
identifiants clients présents dans le checkout.

## Mon index est-il à jour ?

Les écritures réalisées par Datacron réconcilient l'index de façon synchrone. Une réponse
d'écriture réussie avec `indexed: true` suffit comme preuve pour cette modification. Les lectures
adossées à l'index réparent aussi périodiquement les changements de fichiers externes ; il ne faut
donc pas interroger `get_health` après chaque opération.

Appelle `get_health` après des modifications hors Datacron, lorsque la confirmation d'indexation
manque ou lorsque les résultats de recherche paraissent incohérents. Il réalise un scan live
exact : examine `consistent_with_vault`, `stale_entries` et `hash_divergences`. Si l'index est
incohérent, arrête tous les writers, réalise une sauvegarde vérifiée de `.datacron` hors du vault,
puis lance :

```bash
datacron reindex --vault "CHEMIN_VAULT"
```

`reindex` est une maintenance hors ligne. Il valide et publie atomiquement un remplacement
complet, et échoue de manière fermée tant que des sidecars SQLite `-wal` ou `-shm` actifs existent.

## Que supprime et que préserve `datacron setup --reset` ?

Reset supprime exactement deux cibles autorisées sous le vault sélectionné :
`.datacron/VAULT.yaml` et le dossier `.datacron/index/` complet. Le setup recrée ensuite la
configuration et l'index selon les options courantes. Les gardes contre les symlinks et reparse
points empêchent reset de suivre une cible redirigée.

Reset préserve les notes Markdown, les identités stables des notes, l'historique, les journaux
d'opérations, les données d'audit, les logs et le reste du sidecar `.datacron`. Il ne retire ni
les entrées des clients MCP, ni les blocs du protocole mémoire, ni l'application installée, ni un
réglage d'écriture utilisateur. Utilise `unregister` ou `protocol uninstall` pour ces sujets
distincts.

## Quels switches sont disponibles pour une installation Windows silencieuse ?

`/VAULT` est obligatoire en mode silencieux. Les autres switches Datacron sont opt-in :

```bat
Datacron-Setup.exe /VERYSILENT /VAULT="C:\Users\me\Notes" /INDEX /ENABLEWRITE
```

- `/VAULT="CHEMIN"` : sélectionne le vault ; obligatoire pour une installation silencieuse.
- `/INDEX` : construit l'index pendant l'installation. Sans lui, le setup silencieux n'indexe pas.
- `/RESETCONFIG` : supprime la configuration et l'index généré du vault avant le setup.
- `/ENABLEWRITE` : active les outils d'écriture avec l'allowlist `_memory`, `_drafts` et
  `_journal`.
- `/MACHINEWIDEWRITE` : persiste aussi cette allowlist dans l'environnement utilisateur ; ignoré
  sans `/ENABLEWRITE`.

Sans `/RESETCONFIG`, la configuration existante est conservée. Sans `/ENABLEWRITE`, l'installeur
ne passe aucun flag d'écriture au setup et laisse inchangé un environnement utilisateur
d'écriture existant.

## Que supprime le désinstalleur Windows et que laisse-t-il en place ?

Le désinstalleur tente de retirer les entrées MCP de Datacron des configurations utilisateur et
de la configuration projet sous le vault mémorisé par l'installeur. Il retire les blocs du
protocole mémoire Datacron au scope utilisateur, le dossier de l'application et les raccourcis,
l'entrée Datacron du `PATH` utilisateur et l'état de l'installeur dans le registre. Les autres
serveurs MCP des fichiers de configuration partagés sont préservés.

Il ne supprime jamais le vault, les notes Markdown ni le sidecar `.datacron`, y compris la
configuration, l'index, les identités, l'historique, le journal d'opérations, les données d'audit
et les logs. Il laisse aussi inchangée une valeur utilisateur `DATACRON_WRITE_PATHS` existante.
Les blocs de protocole projet installés manuellement et les enregistrements projet d'autres vaults
peuvent rester ; retire-les avec `datacron protocol uninstall` ou `datacron unregister` avant de
désinstaller l'exécutable.

## Où sont les logs et comment diagnostiquer un problème ?

Le setup crée `<vault>/.datacron/logs/`, et `datacron status --vault "CHEMIN_VAULT"` affiche le
vault, le nombre de notes, l'état de l'index, la base d'index et le chemin attendu du log quotidien
dans le sidecar. Commence le diagnostic par cette commande et vérifie qu'elle pointe vers le vault
voulu.

L'emplacement actif du FileLogger est contrôlé par `DATACRON_LOG_DIR` ; son défaut actuel est
`~/.datacron/logs`. Si le dossier de logs du sidecar est vide, vérifie cette variable
d'environnement et le dossier par défaut. Définis `DATACRON_LOG_DIR` sur
`<vault>/.datacron/logs` dans l'environnement du serveur si tu veux colocaliser les logs et le
vault. Les fichiers quotidiens se nomment `datacron_YYYYMMDD.log` ; les warnings sont aussi envoyés
sur stderr. Pour signaler un échec, joins la sortie de `datacron status`, la version de Datacron,
le nom et le scope du client, ainsi que les lignes de log pertinentes après suppression des
secrets.
