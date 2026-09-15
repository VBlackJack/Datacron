# Documentation Datacron

**Français** | [English](../en/index.md)

Point d'entrée de toute la documentation. Datacron est un serveur MCP local qui interroge
et maintient un vault Markdown depuis Claude, sans envoyer le vault complet dans le contexte.

## Démarrer

| Document | Pour quoi |
|---|---|
| [README](../../README.fr.md) | Vue d'ensemble, capacités, mesures actuelles. |
| [Guide d'installation et de configuration](setup.md) | Installer, initialiser un vault, brancher Claude Desktop / Claude Code, variables d'env, activer l'écriture. |
| [Utiliser Datacron avec Ollama](ollama.md) | Relier Ollama au serveur MCP stdio de Datacron avec un pont explicite et des limites de preuve documentées. |
| [Installation sous Windows (installeur)](installation-windows.md) | Installeur `Datacron-Setup.exe` : double-clic, sans Python, enregistrement automatique des clients, réinstallation, silencieux, désinstallation. |
| [Questions fréquentes](faq.md) | Correctifs par symptôme pour le choix du vault, l'écriture, les clients, la fraîcheur de l'index, reset, la désinstallation et les logs. |
| [Guide utilisateur](user-guide.md) | Usage quotidien depuis Claude : recherche, lecture, écriture, supervision, exemples de demandes. |
| [Discipline mémoire](memory-discipline.md) | Initialisation commune, fiches personnes, suivi sourcé et diagnostic des clients. |

## Comprendre le fonctionnement

| Document | Pour quoi |
|---|---|
| [Conventions du vault (SPEC)](spec.md) | Contrat vault : sidecar `.datacron/`, frontmatter, modèle de confiance, wikilinks, chunks, audit, versioning. |
| [Organisation du vault](organization.md) | Bloc `organization` de `VAULT.yaml` : tags, dossiers, gabarits de nom, plafonds de taille, et `datacron reorganize` qui mesure l'écart en lecture seule. |
| [Architecture et surface publique](architecture.md) | Architecture technique et surface exposée. |
| [Contrat de fraîcheur v1](freshness-contract-v1.md) | Garanties de fraîcheur de l'index. |

## Sécurité, intégrité, exploitation

| Document | Pour quoi |
|---|---|
| [Frontière de sécurité](security-boundary.md) | Confinement lecture/écriture, garanties, modèle de menace local. |
| [Scrubber d'intégrité](integrity-scrubber.md) | Détection de corruption silencieuse, sentinelles, passes de scrub. |
| [Santé opérationnelle et durabilité](operational-health.md) | Mode lecture seule certifié, politique de durabilité, `get_health`. |

## Travailler au quotidien

| Document | Pour quoi |
|---|---|
| [Parcours quotidiens et validation mesurée](daily-workflows.md) | Orientation de session, suivi sourcé, progression des écritures et parcours d'archivage, avec la preuve mesurée derrière chacun. |
| [Lire et réorganiser les sections d'une note](note-sections.md) | Sélectionner, déplacer, renommer et supprimer des sections depuis le plan des titres ; configurer les sections lues par une session. |
| [Lire ses notes sans connexion](human-library.md) | Préparer une bibliothèque Markdown navigable du vault pour Obsidian ou un explorateur de fichiers, la réviser, puis appliquer son manifeste. |

## Référence

| Document | Pour quoi |
|---|---|
| [Écritures fiables et qualité de recherche](improvements.md) | Rejeu des écritures ordinaires par `request_id`, contrats d'indexation et mesures de qualité de recherche. |
| [Propositions de contradiction complètes](contradiction-proposals.md) | Comment une proposition de contradiction reste complète dans son budget, avec références sourcées datées et preuves en sandbox. |
| [Stockage et présentation des suivis](follow-up-storage.md) | Ce que `prepare_follow_up` stocke et comment `get_follow_up` le présente. |
| [Champs audités du frontmatter](frontmatter-audit.md) | Les champs de cycle de vie qu'une écriture de frontmatter enregistre dans le journal d'opérations. |
| [Durée de validité des tokens de proposition](proposal-token-lifetime.md) | Un token de proposition `cs2:` porte une date de proposition, pas une durée de vie. |

## Notes de version

| Document | Pour quoi |
|---|---|
| [Datacron 2026.0913.02 : bibliothèque hors ligne](offline-library-release-notes.md) | La version qui a ajouté la bibliothèque Markdown locale. |
| [Version à venir : correctifs du test de surface](surface-fixes-release-notes.md) | Les fils de titres aux vrais niveaux Markdown et la réindexation qu'ils imposent. |
