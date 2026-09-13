# Datacron 2026.0913.02

[English](../en/offline-library-release-notes.md) | **Français**

Cette version ajoute une bibliothèque Markdown locale pour retrouver et lire les
notes dans Obsidian sans connexion Internet. Le
[guide de lecture hors ligne](human-library.md) explique comment préparer un
aperçu, revoir ses sources et ses liens, puis appliquer le manifeste exact pendant
une maintenance.

## Nouveautés

- L'accueil et les cartes des dossiers mènent aux sujets, personnes, procédures,
  actions et références historiques.
- Les recettes de consolidation préparent des synthèses sourcées et des découpages
  de sections, conservent les originaux et protègent les notes avec des cases ouvertes.
- Les pièces jointes locales référencées sont copiées dans les limites configurées.
  Les sites externes restent externes ; leurs synthèses utiles doivent être locales.
- Le contexte de session propose des priorités, un budget adaptatif et un contrat en cache.
- Le suivi des écritures et les conseils de récupération distinguent note enregistrée
  et échec de son indexation.
- Le masquage du contexte, les identités dupliquées et les anciens index d'identités
  bénéficient de contrôles renforcés.
- Les écritures concurrentes réservent le verrou SQLite avant le contrôle d'identité,
  ce qui évite les conflits de conversion des verrous entre plusieurs clients.

## Utilisation

La bibliothèque s'entretient explicitement : elle ne programme aucune revue et
n'archive aucune note de sa propre initiative. La configuration existante et le
corps des notes originales sont conservés. Vérifier la fraîcheur des sources avant
d'appliquer un lot, arrêter les autres clients Datacron et conserver une sauvegarde vérifiée.

Le workflow de validation teste une installation et une réinstallation silencieuses
sur une VM Windows jetable, avec un PATH limité à System32. Il ne remplace pas une
évaluation interactive de l'installeur ou un test sur une image Windows grand public vierge.

Voir les [usages quotidiens](daily-workflows.md) pour la pratique courante.
