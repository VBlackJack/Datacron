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

## Migration locale mesurée

Le 2026-09-10, le candidat Windows local `2026.0910.01` a reconstruit un vault de
2 431 notes indexées et 95 094 chunks en 260,197 secondes de durée totale (4 min 20 s).
La commande de reindex a annoncé 258,558 secondes et la génération est passée de
2873 à 2874. La comparaison avec l'index sauvegardé relève 22 364 identifiants de
chunks remplacés dans 641 notes.

Avant les écritures de recette, le checksum Markdown était identique avant et après
migration, ainsi que le SHA256 séparé de `VAULT.yaml`. Le nouvel index ne signalait
aucune entrée périmée, divergence de hash, incohérence d'identité ou erreur de parsing
du frontmatter. Cette mesure locale ne garantit pas la durée pour d'autres vaults.

Aucun véritable token de proposition n'a été conservé avant ce reindex réel : le
refus d'un token antérieur à la migration n'a donc pas été testé sur ce vault. Un
token synthétique daté de 2020 a vérifié l'absence de TTL et un token inconnu a
vérifié le chemin d'erreur ; aucun des deux ne valide ce scénario historique.
`test_proposal_reindex.py` couvre le refus lorsque les identifiants de chunks
changent et la conservation lorsqu'ils restent identiques. Conserver un véritable
token avant le prochain reindex contrôlé permettra de vérifier ce cas en migration
réelle.

Le candidat local reste `2026.0910.01` ; la version publique prévue est
`2026.0910.02`. Un vault déjà reconstruit avec `.01` n'a pas besoin d'un nouveau
reindex pour ce seul incrément de version.
