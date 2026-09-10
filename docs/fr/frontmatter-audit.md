# Champs audités du frontmatter

Pour `set_frontmatter`, `updated.fields` dans le reçu et `parameters.fields` dans le
journal décrivent les champs de cycle de vie réellement modifiés, dans le même ordre.
Les champs demandés déjà à la bonne valeur sont exclus. Le timestamp automatique `updated`
n'entre pas dans cette liste. Une demande sans changement de cycle de vie rend donc
une liste vide dans le reçu et une chaîne vide pour ce champ du journal.

[Translation](../en/frontmatter-audit.md)
