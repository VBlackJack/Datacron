# Stockage et présentation des suivis

[Translation](../en/follow-up-storage.md)

`prepare_follow_up` conserve le résumé, l'extrait source et le contexte d'identité d'origine
dans le JSON, sans enveloppe de présentation. Le discriminant stocké `text_format: raw`
distingue ces entrées des anciennes entrées enveloppées. Quand le masquage en lecture est
actif (`DATACRON_REDACT_SECRETS` vaut `retrieval` ou `all`), un plan dont un champ persisté
contient un texte ressemblant à un secret est refusé avec le code `follow_up_sensitive_content`,
et le message nomme le champ ; le reste de la note source n'est pas examiné. Le propriétaire
conserve l'échappement existant des marqueurs de contrôle.

`get_follow_up` présente `source_excerpt`, `summary` et `identity_basis` chacun dans une
enveloppe. L'extrait est étiqueté avec `source_path`, les deux autres avec la note lue. Les
anciennes entrées stockées avec une enveloppe en ressortent avec une seule. La lecture échappe
toujours les marqueurs de contrôle et, selon la même politique, masque les secrets : elle ne
constitue donc pas un export exact pour ces textes. Le texte ordinaire conserve Unicode et
espaces à l'intérieur de l'enveloppe.

Les digests historiques et les chaînes de révisions sont vérifiés avant présentation.
Les anciennes enveloppes reconnues sont retirées uniquement de la projection de lecture ;
les octets persistés restent inchangés et le rejeu des anciennes révisions identiques reste
possible. Dans les nouvelles entrées brutes, une enveloppe littérale reste une donnée.

Aucun nettoyage de masse n'est exécuté. Une proposition de migration doit inventorier les
entrées concernées et préserver l'intégrité des révisions et digests avant accord explicite.
