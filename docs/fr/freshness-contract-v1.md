# Contrat de fraicheur v1

**Français** · [English](../en/freshness-contract-v1.md)

## `freshness-contract-v1`

Ce contrat definit le hash d'une note Markdown. L'entree est la sequence ordonnee
exacte d'octets retournee par une lecture binaire reussie du fichier. L'algorithme
est SHA-256 et la sortie est une chaine de 64 caracteres hexadecimaux ASCII en
minuscules. Il n'y a aucune normalisation: ni conversion des fins de ligne, ni
suppression de BOM, ni re-encodage texte, ni ajout ou retrait du newline final, ni
normalisation Unicode. La frontiere normative est `bytes -> hash`; l'implementation
de reference Datacron est `sha256_bytes(path.read_bytes())`.

`get_note` retourne ce digest sous `note_content_hash` et conserve `content_hash`
comme alias compatible. Il retourne aussi `content_hash_contract` avec la valeur
`freshness-contract-v1`. Un consommateur autonome peut appliquer cette specification
localement: Cortex n'appelle jamais Datacron au runtime pour decider de sa fraicheur.

Le manifeste partage `tests/fixtures/freshness-contract-v1.json` contient les vecteurs
Base64 pour LF, CRLF, BOM et Unicode NFD. Les tests les ecrivent en binaire avant le hash.

## Hash de chunk derive

Pour une lecture par `chunk_id`, `get_note` retourne aussi `chunk_content_hash`: le
`content_hash` deja indexe du chunk, calcule a partir de `chunk.content.encode("utf-8")`.
Le chunk est derive du parsing; ce digest n'est pas celui d'une tranche d'octets source.

## `sha256-path-content-hash-rollup-v1`

`vault_checksum` est un contrat distinct et inchange. Il calcule un rollup SHA-256 des
chemins relatifs tries et des hashes de note deja calcules: chemin UTF-8, NUL, hash ASCII,
puis LF. Il sert de checksum ponctuel du vault, pas de hash d'une note.
