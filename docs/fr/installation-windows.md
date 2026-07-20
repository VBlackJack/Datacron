# Installation sous Windows (installeur)

**Français** | [English](../en/installation-windows.md)

Cette page décrit l'installeur graphique `Datacron-Setup.exe` : un double-clic, sans
Python et sans terminal, qui installe Datacron pour ton compte utilisateur et
**enregistre automatiquement Datacron dans les clients IA détectés** et y installe son
protocole mémoire (Claude Desktop, Claude Code, Cursor, Gemini CLI, Codex CLI, Windsurf,
VS Code).

Si tu préfères la ligne de commande (`datacron setup`), va plutôt sur le
[guide d'installation et de configuration](setup.md).

> Datacron ne modifie jamais tes notes sans que tu l'actives explicitement, et n'envoie
> rien vers un service cloud. Il ajoute seulement un dossier `.datacron/` à côté de tes
> notes, enregistre son serveur MCP et ajoute uniquement un bloc d'instructions balisé dans
> les règles globales prises en charge.

## 1. Installer

1. Télécharge `Datacron-Setup.exe` depuis la page **Releases** du dépôt.
2. Double-clique dessus. L'installation est **par utilisateur** : aucun droit
   administrateur, aucune élévation (UAC).
3. **Choisis ton vault** : le dossier de notes Markdown que tu ouvres déjà dans
   Obsidian. Datacron y créera un sous-dossier `.datacron/` (index, config, audit).
4. Laisse cochée la case **Indexer maintenant** pour construire l'index tout de
   suite (recommandé), ou décoche-la pour le faire plus tard.
5. **Outils d'écriture** (optionnel) : les deux cases sont décochées par défaut,
   l'installeur n'active donc pas l'écriture. Coche **Activer les outils d'écriture
   confinés** pour autoriser l'écriture uniquement dans les sous-dossiers `_memory`,
   `_drafts` et `_journal`. Coche **Appliquer aussi la liste d'autorisation à mon
   environnement utilisateur** pour la propager également aux futurs clients MCP.
   Une variable utilisateur `DATACRON_WRITE_PATHS` existante reste inchangée quand
   cette option est décochée.
6. Termine. L'installeur ajoute `datacron.exe` à ton **PATH utilisateur**, crée les
   raccourcis du menu Démarrer, puis lance la configuration : il enregistre Datacron
   dans chaque client IA détecté, installe les instructions mémoire globales prises en charge
   et indexe le vault. Cursor affiche encore une étape manuelle dans **Settings > Rules** ;
   Claude Desktop reçoit les instructions directement pendant l'initialisation MCP.

Après l'installation, redémarre Claude Desktop (ou ton client) pour qu'il charge le
serveur Datacron.

## 2. Le choix du vault

Le vault est **ton dossier de notes** : la source de vérité. L'installeur n'y touche
pas, il ajoute seulement `.datacron/`. Si tu pointes vers un dossier qui n'existe pas
encore, il est créé.

## 3. Réinstaller (Garder ou Réinitialiser)

Si une configuration Datacron existe déjà dans le vault choisi, l'installeur propose :

- **Garder** (par défaut) : rien n'est écrasé, ta configuration et ton index sont
  conservés.
- **Réinitialiser** : efface la config (`VAULT.yaml`) et l'index, puis les
  reconstruit. Sont **préservés** : tes notes `.md`, l'identité stable des notes,
  l'historique et le journal d'audit.

Si tu réinstalles en pointant vers un **autre vault**, l'installeur désenregistre
d'abord l'ancien vault de tes clients, pour éviter les entrées fantômes.

## 4. Raccourcis du menu Démarrer

- **Datacron Status** : ouvre une console sur l'état du vault, de l'index et de la santé.
- **Datacron Setup** : relance la configuration (par exemple pour réenregistrer un client).

## 5. Installation silencieuse (déploiement)

Pour un déploiement scripté, `/VAULT=` est **obligatoire** en mode silencieux :

```bat
Datacron-Setup.exe /VERYSILENT /VAULT="C:\Users\moi\Notes"
```

Options :

- `/RESETCONFIG` : réinitialise la config et l'index (au lieu de garder).
- `/INDEX` : indexe pendant l'installation. Sans ce commutateur, l'index n'est pas
  construit à ce moment-là.
- `/ENABLEWRITE` : active les outils d'écriture confinés (`_memory`, `_drafts`,
  `_journal`). Sans ce commutateur, l'installeur ne transmet aucune liste
  d'autorisation au setup ; une variable utilisateur `DATACRON_WRITE_PATHS` existante
  reste inchangée.
- `/MACHINEWIDEWRITE` : applique aussi la liste d'autorisation d'écriture à
  l'environnement utilisateur. Ignoré sans `/ENABLEWRITE`.

## 6. Désinstaller

Désinstalle depuis **Paramètres Windows > Applications**. L'opération, dans l'ordre :
retire l'entrée MCP `datacron` de tes clients, retire les blocs d'instructions gérés par
Datacron, retire `datacron.exe` de ton PATH utilisateur, puis supprime le programme.
**Ton vault et tes notes ne sont jamais touchés.**

## 7. Vérifier

- Depuis Claude, demande un `get_health`, ou
- lance **Datacron Status** au menu Démarrer (ou `datacron status --vault "<vault>"`).
