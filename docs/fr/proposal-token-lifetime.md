# Durée de validité des tokens de proposition

Les tokens de `contradiction_scan` n'expirent pas avec le temps. La date dans
`cs2:YYYY-MM-DD:<sha256>` est celle de la proposition utilisée dans la mise à jour,
pas une TTL. Un token de la veille reste confirmable si la proposition se recalcule à l'identique.

La confirmation rescane les candidats courants et vérifie la cible contre son hash indexé
courant. Une proposition inconnue ou modifiée est refusée. Le `expected_hash` retourné
protège l'écriture explicite suivante par compare-and-set : une modification de la cible
après confirmation fait échouer cette écriture. L'âge du token n'ajoute aucune garantie
de fraîcheur ; une proposition reste une proposition tant que son writer n'a pas réussi.

[Translation](../en/proposal-token-lifetime.md)
