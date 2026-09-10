# Durée de validité des tokens de proposition

Les tokens de `contradiction_scan` n'expirent pas avec le temps. La date dans
`cs2:YYYY-MM-DD:<sha256>` est celle de la proposition utilisée dans la mise à jour,
pas une TTL. Un token de la veille reste confirmable si la proposition se recalcule à l'identique.

La confirmation rescane les candidats courants et vérifie la cible contre son hash indexé
courant. Une proposition inconnue ou modifiée est refusée. Le `expected_hash` retourné
protège l'écriture explicite suivante par compare-and-set : une modification de la cible
après confirmation fait échouer cette écriture. L'âge du token n'ajoute aucune garantie
de fraîcheur ; une proposition reste une proposition tant que son writer n'a pas réussi.

L'absence d'expiration temporelle ne garantit pas la validité après une migration
d'index. L'identité de la proposition inclut les identifiants des chunks cible et source.
Un reindex qui change l'un d'eux peut rendre un ancien token introuvable malgré des
octets de notes inchangés. La migration D1 retourne `proposal_token_stale_or_unknown`
et demande un nouveau scan suivi d'une revue, sans appel d'écriture. Les tokens `cs2`
ne portent aucune génération d'index : ce refus ne distingue pas une migration d'un
autre changement de candidat ou d'un token inconnu. Une reconstruction qui conserve
l'identité de la proposition ne l'invalide pas du seul fait d'avancer la génération.

[Translation](../en/proposal-token-lifetime.md)
