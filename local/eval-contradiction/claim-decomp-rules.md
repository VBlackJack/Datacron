# Règles de décomposition en claims — v1

Date : 2026-07-13

## Contrat

Le provider découpe uniquement `title` et `body` du `corpus.json` figé. Il ne lit pas le vault, ne
fait aucun appel réseau et n'utilise aucun modèle. Chaque segment retenu devient un claim normalisé :

`{sujet, portée, polarité, valeur_ou_état, contexte_temporel}`.

Toutes les listes lexicales et tous les seuils sont versionnés dans `claim-decomp-rules.json` et
`claim-decomp-config.json`. Le module Python contient le mécanisme générique, pas de paire, chemin,
valeur métier ou budget dissimulé.

## Extraction

1. Découper le Markdown par lignes puis ponctuation, retirer les marqueurs de présentation.
2. Passer en minuscules ASCII et ramener les variantes déclarées vers un terme canonique.
3. Retenir les segments qui portent une polarité, une valeur/état ou un marqueur de claim.
4. Construire le sujet avec les termes significatifs du segment, du titre et des tags, après retrait
   des stopwords et marqueurs opératoires.
5. Classer la portée parmi obligation, activation, état remplacé, seuil/valeur ou assertion.
6. Extraire séparément la polarité, les valeurs/états et les marqueurs temporels.

Les alias décrivent des concepts réutilisables (licence, localisation, mise à jour, index, certificat,
conteneur, framework, etc.). Ils ne contiennent ni chemin de note ni couple attendu.

## Appariement et conflit potentiel

Les termes de sujet trop fréquents dans le corpus sont ignorés. Deux claims deviennent compatibles
s'ils partagent assez de poids IDF et d'overlap, et si leurs portées sont identiques ou explicitement
compatibles dans la configuration.

Une paire de notes est émise uniquement lorsqu'au moins un appariement compatible présente :

- une polarité positive opposée à une polarité négative ; ou
- deux ensembles distincts de valeur/état non vides.

Le score combine conflit, overlap de sujet, poids des termes partagés et bonus de portée identique.
Il sert seulement à classer les candidats, jamais à prononcer une contradiction.

## Budget

`max_pool_pairs` est une garde de mesure, pas une limite appliquée au générateur. Le pool plein est
toujours compté avant décision ; dépasser 400 paires disqualifie la configuration même si son rappel
est élevé.
