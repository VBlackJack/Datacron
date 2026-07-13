# Règles de durcissement du générateur — v2

Date : 2026-07-13

## Anti-fuite

Les règles v2 ne contiennent aucun chemin de note, identifiant synthétique ou couple labellisé. Elles
décrivent des mécanismes réutilisables : modalité déontique, exclusivité/concurrence, cycle de vie d'une
décision, forme de stockage, autorité de la donnée, cardinalité et relation `supersedes`.

Le harnais scanne le provider, le harnais lui-même, la configuration et ce fichier JSON contre toutes
les identités des positifs labellisés. Une correspondance exacte fait échouer la garde anti-fuite.

## Polarité déontique

La normalisation applique d'abord les alias les plus longs. Ainsi, « ne doit pas » / `must not` restent
une interdiction et ne sont pas partiellement transformés en obligation par `doit` / `must`.

Pour la portée `obligation`, trois polarités sont distinguées :

- `required` : doit, devrait, must, should, shall, règle bloquante ;
- `forbidden` : ne doit pas, cannot, prohibited, exclusivité ;
- `permitted` : peut, may, allowed, acceptable.

Les couples forbidden/required et forbidden/permitted constituent un conflit déontique potentiel sur
un sujet compatible. Le mécanisme ne contient aucune connaissance d'une paire du jeu de test.

## États révisés et valeurs

Les nouveaux états normalisés couvrent les décisions corrigées ou invalidées, les travaux résolus,
réouverts ou livrés, l'exécution sérialisée/concurrente, les formats fichier/magasin, l'autorité
Markdown/store dérivé et les cardinalités un/plusieurs. Ils ne suffisent jamais seuls : les claims
doivent encore partager un sujet rare et avoir une portée compatible.

Une relation frontmatter `supersedes` abaisse légèrement le seuil d'appariement et ajoute un bonus de
ranking. Elle n'émet aucune paire sans conflit de polarité ou de valeur/état, afin de ne pas assimiler
toute supersession à une contradiction.

## Budget

Le seuil de score reste explicite dans `claim-decomp-v2-config.json`. Le pool plein est mesuré avant les
gardes : 400 paires maximum et ratio maximal de 25 % des 2 556 paires possibles. Aucun cutoff n'est
utilisé pour masquer une explosion du générateur.
