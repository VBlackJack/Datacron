# Stockage et présentation des suivis

[Translation](../en/follow-up-storage.md)

`prepare_follow_up` conserve le résumé, l'extrait source et le contexte d'identité d'origine
dans le JSON, sans enveloppe de présentation. Le discriminant stocké `text_format: raw`
distingue ces entrées des anciennes entrées enveloppées. Les plans contenant des secrets
sont refusés ; le propriétaire conserve l'échappement existant des marqueurs de contrôle.

`get_follow_up` présente l'extrait dans une seule enveloppe étiquetée avec `source_path`.
Le résumé et le contexte d'identité ne portent pas d'enveloppe de source vault. La lecture
échappe toujours les marqueurs de contrôle et masque les secrets : elle ne constitue donc
pas un export exact pour ces textes. Le texte ordinaire conserve Unicode et espaces.

Les digests historiques et les chaînes de révisions sont vérifiés avant présentation.
Les anciennes enveloppes reconnues sont retirées uniquement de la projection de lecture ;
les octets persistés restent inchangés et le rejeu des anciennes révisions identiques reste
possible. Dans les nouvelles entrées brutes, une enveloppe littérale reste une donnée.

Aucun nettoyage de masse n'est exécuté. Une proposition de migration doit inventorier les
entrées concernées et préserver l'intégrité des révisions et digests avant accord explicite.
