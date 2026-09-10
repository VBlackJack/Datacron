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

Les tokens de proposition de contradiction consomment les identifiants de chunks :
leur identité inclut ceux de la cible et de la source. Les tokens des candidats affectés
ne sont plus confirmables après cette migration, même si les octets des notes restent
identiques. La confirmation retourne `proposal_token_stale_or_unknown`, cite le reindex
et demande un nouveau `contradiction_scan(mode="scan")`, suivi d'une revue. Aucun appel
d'écriture n'est retourné. Le format `cs2` ne porte aucune génération d'index : le serveur
ne peut donc pas attribuer avec certitude ce refus au reindex. Une proposition inchangée
reste confirmable après une reconstruction qui conserve ses identifiants de chunks.
Aucune TTL temporelle n'est ajoutée.
