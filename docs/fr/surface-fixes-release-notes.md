# Version à venir : correctifs du test de surface

[Translation](../en/surface-fixes-release-notes.md)

## Hiérarchie des titres

Le fil suit désormais les niveaux Markdown réels. Les H2 soeurs ne deviennent plus les
enfants du premier H2 lorsque le corps ne contient pas de H1 ; les niveaux sautés
préservent aussi les frontières entre sections soeurs. Le titre du frontmatter reste
un signal de recherche séparé, sans devenir une racine virtuelle.

Les identifiants des chunks concernés changent. Lance un `datacron reindex` complet après
l'installation de la version corrigée, puis récupère des références de chunks fraîches.
Les identifiants et les octets des notes ne changent pas. La lecture ordinaire de notes
inchangées ne migre pas un index construit avec l'ancien chunker.
