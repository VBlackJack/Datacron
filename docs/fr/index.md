# Documentation Datacron

**Français** | [English](../en/index.md)

Point d'entrée de toute la documentation. Datacron est un serveur MCP local qui interroge
et maintient un vault Markdown depuis Claude, sans envoyer le vault complet dans le contexte.

## Démarrer

| Document | Pour quoi |
|---|---|
| [README](../../README.md) | Vue d'ensemble, capacités, mesures actuelles. |
| [Guide d'installation et de configuration](setup.md) | Installer, initialiser un vault, brancher Claude Desktop / Claude Code, variables d'env, activer l'écriture. |
| [Installation sous Windows (installeur)](installation-windows.md) | Installeur `Datacron-Setup.exe` : double-clic, sans Python, enregistrement automatique des clients, réinstallation, silencieux, désinstallation. |
| [Guide utilisateur](user-guide.md) | Usage quotidien depuis Claude : recherche, lecture, écriture, supervision, exemples de demandes. |

## Comprendre le fonctionnement

| Document | Pour quoi |
|---|---|
| [Conventions du vault (SPEC)](spec.md) | Contrat vault : sidecar `.datacron/`, frontmatter, modèle de confiance, wikilinks, chunks, audit, versioning. |
| [Architecture et surface publique](architecture.md) | Architecture technique et surface exposée. |
| [Décisions tranchées v2.1](decisions-v2.1.md) | Choix de conception arrêtés et leurs justifications. |
| [Contrat de fraîcheur v1](freshness-contract-v1.md) | Garanties de fraîcheur de l'index. |

## Sécurité, intégrité, exploitation

| Document | Pour quoi |
|---|---|
| [Frontière de sécurité](security-boundary.md) | Confinement lecture/écriture, garanties, modèle de menace local. |
| [Scrubber d'intégrité](integrity-scrubber.md) | Détection de corruption silencieuse, sentinelles, passes de scrub. |
| [Santé opérationnelle et durabilité](operational-health.md) | Mode lecture seule certifié, politique de durabilité, `get_health`. |
