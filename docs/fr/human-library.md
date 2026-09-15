# Lire ses notes sans connexion

[English](../en/human-library.md) | **Français**

Datacron prépare une bibliothèque Markdown consultable dans Obsidian ou un autre
lecteur local. L'accueil mène aux cartes des dossiers, personnes, procédures,
cases ouvertes et références historiques. Les cartes affichent des titres lisibles ;
des groupes configurables séparent notamment les sujets professionnels et personnels.
La lecture ne nécessite ni plugin communautaire, ni base de données, ni modèle,
ni connexion Internet.

Le vault source reste canonique. `preview/` est une copie datée de revue, pas une
seconde base à modifier. Ouvrir ce dossier comme vault Obsidian, puis sa note d'accueil.
Les pièces jointes locales référencées sont copiées lorsqu'elles existent, sont
admises et respectent les limites de taille. Les sites externes ne sont pas téléchargés.
Vérifier les références non résolues avant de compter sur leur disponibilité hors ligne.
L'export conserve les contenus locaux sélectionnés, y compris privés : lui appliquer
les mêmes restrictions d'accès qu'au vault source.

Utiliser le vrai nom du fichier comme cible d'un wikilink, par exemple
`[[etat-projet|État du projet]]`, ou un chemin relatif au vault tel que
`[[projets/etat-projet|État du projet]]`. Le texte après `|` est le libellé lisible.
Un lien visant directement un titre de frontmatter ou un alias peut fonctionner
dans Datacron sans être résolu par Obsidian. L'audit de la bibliothèque applique
les règles de Datacron : vérifier aussi les anciens liens dans le lecteur choisi.
La préparation conserve les liens sources. Corriger leurs cibles par un manifeste
d'organisation relu lorsque nécessaire, en gardant libellés et identités des notes.

L'export ne contient que les notes du périmètre choisi. Un lien vers une note locale
située ailleurs dans le vault complet peut donc fonctionner dans Obsidian alors que
sa cible est absente de l'export. Vérifier ces limites dans le rapport de revue avant
d'utiliser une copie autonome. Ouvrir le vault local complet conserve l'accès à ces notes.

Pour un complément Confluence retrouvé via Cortex, conserver une synthèse locale
courte avec le lien source, la date de mise à jour du document et la date de lecture.
La confronter aux décisions récentes du projet avant de présenter une ancienne
procédure comme actuelle. Un index récemment actualisé ne prouve pas que sa source
reste valide. Les références externes restent indisponibles hors ligne tant que
leur contenu utile n'a pas été conservé localement.

## Préparer la navigation

Créer un fichier JSON hors du vault. Adapter chemins et tags à la politique existante ;
cet exemple n'installe pas une nouvelle classification :

```json
{
  "scope": "_memory",
  "home": "_memory/accueil.md",
  "tags": ["memory/meta"],
  "language": "fr",
  "areas": {
    "Professionnel": "_memory/subjects/pro",
    "Personnel": "_memory/subjects/perso"
  },
  "max_note_chars": 24000,
  "max_section_chars": 8000
}
```

Exécuter avec ses propres chemins absolus :

```text
datacron library audit --vault VAULT --options OPTIONS.json
datacron library prepare --vault VAULT --options OPTIONS.json --output NOUVEAU_DOSSIER
datacron library check --vault VAULT --output NOUVEAU_DOSSIER
```

`--vault` est facultatif : sans lui, les commandes utilisent `DATACRON_VAULT_ROOT`, puis le
dossier courant s'il contient `.datacron/VAULT.yaml`, comme les autres commandes du vault.
Chaque commande sort avec 0 en cas de succès et 2 quand le vault, les options, la recette ou
le bundle ne peuvent pas être lus ou sont invalides ; `--help` liste toutes les options.

`audit` affiche un JSON sans écrire de journal, d'identifiant ou d'index. `prepare`
exige un nouveau dossier extérieur au vault. Il produit :

- `preview/` : notes sources, pièces jointes copiées et navigation proposée.
- `review.md` et `changes.diff` : constats, raisons éditoriales et différences exactes.
- `audit.json` : tailles, doublons possibles et liens non résolus, ancres comprises.
- `subject-template.md` : modèle court état/actions/décisions/références/historique.
- `snapshot.json` : empreintes des sources et de l'aperçu, périmètre explicite.
- `manifest.json` et `payloads/` : bundle d'organisation adressé par contenu.

L'accueil pointe vers les **sources des actions**, sans recopier les cases.
Le comptage couvre les cases Markdown ouvertes dans les listes, pas les engagements
exprimés uniquement en prose ou dans un tableau. `state_tags`, `people_tags` et
`procedure_tags` configurent les catégories. L'âge seul ne déclenche aucun archivage.
L'historique exige un signal explicite d'archive, d'invalidation ou de remplacement.
Une vérification absente classe la référence à vérifier ; une date de vérification
ne prouve jamais sa validité.

Limites configurables par défaut : 10 000 notes, 2 Mio par note, 32 Mio par pièce
jointe et 256 Mio pour l'aperçu. Le manifeste possède aussi ses limites, notamment
512 opérations : travailler par périmètres plus petits si nécessaire. Les cartes
gardent des noms de fichiers stables. L'actualisation refuse d'écraser une page
personnelle ou une page produite dont le corps a été modifié. Modifier les sources,
ou déplacer les ajouts personnels dans une autre note avant l'actualisation.

`folder_labels` associe des chemins exacts de dossiers à des libellés personnalisés.

## Consolider, découper et archiver

`datacron library split --vault VAULT --options OPTIONS.json --source NOTE_RELATIVE.md`
affiche une recette JSON proposant une note par section H2, y compris Setext.
Les titres identiques reçoivent des destinations numérotées distinctes. L'original,
les liens entrants et les ancres restent présents. Les notes contenant des cases
ouvertes demandent une préparation éditoriale manuelle pour ne pas dupliquer les actions.
Le contexte placé avant le premier H2 reste dans la source liée : vérifier que chaque
extrait contient le contexte nécessaire avant approbation.

Pour une synthèse, fusion ou remise au propre, préparer une recette explicite :

```json
{
  "notes": [{
    "target": "_memory/subjects/perso/exemple/exemple-guide.md",
    "title": "Exemple : guide pratique",
    "body": "# Guide pratique\n\nSynthèse sourcée à relire.",
    "tags": ["memory/fact", "project/exemple"],
    "sources": [{"path": "_memory/subjects/perso/exemple/exemple.md", "sha256": "SHA256_EXACT_DE_LA_SOURCE"}],
    "rationale": "Regrouper les informations de référence dispersées.",
    "archive_sources": []
  }]
}
```

La source doit porter son identité stable dans le frontmatter. Fournir l'empreinte
exacte issue de l'audit, puis ajouter `--recipe RECETTE.json` à `library prepare`.
Plusieurs sources permettent une fusion ; plusieurs sorties permettent un découpage.
Les nouvelles notes portent leur provenance et une confiance faible avant revue.
Une réécriture crée une référence liée ; chemins, titres, alias, ancres et corps des
originaux restent conservés. Les déplacements et renommages physiques restent une
opération distincte par manifeste d'organisation.

`archive_sources` constitue une proposition explicite. Chaque source doit être une
révision exactement référencée et ne contenir aucune case ouverte. La préparation
modifie ses propriétés et renseigne les liens vers les nouvelles références ; les
octets du corps, fins de ligne comprises, restent identiques. Aucune source n'est supprimée.
Les sorties éditoriales doivent pointer vers les actions existantes plutôt que recopier
leurs cases ouvertes. Une ressemblance de contenu constitue un candidat à examiner,
pas la preuve d'un doublon de sens.

## Relire et appliquer

Relire sources, aperçu et différences. Vérifier le sens des décisions, incertitudes,
responsables, dates et engagements. Les contrôles prouvent la fraîcheur des octets et
la validité mécanique, pas la vérité sémantique. Ils ne résolvent pas automatiquement
les contradictions et ne déduisent pas qu'un engagement est terminé.

`library check` refuse les changements de liste de sources, d'octets des sources ou
pièces jointes, d'aperçu et les manifestes invalides. Dans un bundle éditorial, des
remplacements identiques des sources conservées attachent leurs révisions au contrôle
CAS de la transaction. La navigation reste un inventaire daté à actualiser.

Appliquer par `apply_organization_manifest` : validate, revue de l'empreinte et du
jeton retournés, puis apply du même bundle avec ce jeton. Arrêter les autres writers
et vérifier une sauvegarde séparée avant application. Contrôler reçu et indexation,
puis relire les notes canoniques modifiées. En cas de commit interrompu, rejouer la
requête et le jeton identiques selon la [documentation d'organisation](organization.md).
L'historique permet de restaurer les notes remplacées ; l'aperçu ne constitue pas une
sauvegarde vérifiée du vault et de son sidecar.

## Recette hors ligne

Couper le réseau, ouvrir `preview/` dans Obsidian et retrouver depuis l'accueil une
procédure, l'état d'un projet et la source d'une décision. Ouvrir les pièces jointes
locales et l'historique. Contrôler aussi les titres ambigus et les liens vers une section.
Les tests automatiques vérifient fichiers, liens, conservation et parcours MCP
d'application/rejeu ; ils ne prétendent pas avoir testé l'interface native d'Obsidian.
