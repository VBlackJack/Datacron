# Guide utilisateur

**Français** | [English](../en/user-guide.md)

Ce guide explique comment se servir de Datacron au quotidien depuis Claude, une fois
l'installation faite (voir [Guide d'installation](setup.md)). Datacron n'est pas une
application avec une interface : c'est un serveur MCP que ton client (Claude Desktop ou
Claude Code) interroge. Tu travailles donc en langage naturel, et Claude appelle les outils
Datacron pour toi.

## Le modèle mental en une minute

Datacron indexe ton dossier de notes et, au lieu d'envoyer tout le vault dans le contexte,
il renvoie à Claude uniquement les notes ou fragments (chunks) pertinents. Concrètement :

- Tu poses une question ou demandes une action sur tes notes.
- Claude choisit le bon outil Datacron (recherche, lecture, écriture...).
- Datacron renvoie un résultat borné (nombre et budget token limités), enveloppé dans
  `<vault_content>...</vault_content>`.
- Tes notes restent des fichiers Markdown normaux, modifiables à la main à tout moment.

## Les outils, par usage

### Lire et chercher

| Outil | À quoi il sert |
|---|---|
| `list_notes` | Liste paginée des notes, filtrable par dossier, par tags et par frontmatter de premier niveau ; renvoie ULID, titre, tags, alias et dates. |
| `get_note` | Lit une note précise par ULID, par identifiant de chunk ou par chemin relatif ; contenu paginé, chunk isolé, ou plan des titres. |
| `search_text` | Recherche BM25 sur l'index FTS5 : snippets classés, notes obsolètes démotées par défaut. |
| `search_regex` | Recherche littérale par expression régulière via ripgrep, résolue vers les chunks indexés. |
| `get_backlinks` | Renvoie les chunks dont les wikilinks pointent vers un ULID ou un alias donné. |

Par exemple, `list_notes(frontmatter={"confidence": "needs_verification", "origin": "ai"})`
renvoie les notes qui correspondent aux deux paires. Les clés et les valeurs de frontmatter
sont comparées sans tenir compte de la casse ; une liste correspond si l'un de ses éléments
correspond. Une requête peut contenir au maximum huit paires.

### Écrire (si activé)

Ces outils ne fonctionnent que si `DATACRON_WRITE_PATHS` est défini (voir
[Guide d'installation, §8](setup.md#8-activer-lécriture-optionnel)). Ils sont confinés,
atomiques, historisés et audités.

| Outil | À quoi il sert |
|---|---|
| `create_note_ai` | Crée une nouvelle note typée, sans écraser de fichier existant. |
| `append_journal` | Ajoute une entrée sous un titre existant d'une note. |
| `set_frontmatter` | Met à jour les champs de cycle de vie sans toucher au corps Markdown. |
| `patch_note_section` | Remplace le contenu sous un titre existant, avec contrôle de version (CAS). |
| `revert_note` | Restaure les octets exacts d'une version conservée dans l'historique. |

### Superviser

| Outil | À quoi il sert |
|---|---|
| `get_health` | État réel : fraîcheur de l'index, intégrité, checksum, durabilité, invariants. |
| `get_note_history` | Métadonnées des opérations validées d'une note, sans lire le contenu historique. |
| `audit_query` | Interroge le journal d'opérations par période, outil ou note, en lecture seule. |

Trois ressources MCP complètent ces outils : `datacron://vault/map` (carte du vault),
`datacron://vault/info` (métadonnées) et `datacron://policy/active` (politique active).

## Comment fonctionne la recherche

`search_text` combine plusieurs signaux, ce qui explique pourquoi les résultats ne sont pas
un simple « match de mots » :

- **FTS5 / BM25** pour le score lexical de base.
- **Query-expansion FR↔EN** configurée dans `VAULT.yaml` : par exemple « sauvegarde »
  remonte aussi les notes qui parlent de « backup ».
- **Re-rank temporel conservateur** :
  - une note citée dans le champ `supersedes` d'une autre est fortement démotée ;
  - une note portant `invalid_at` est démotée de façon identique ;
  - `confidence: low` et `confidence: needs_verification` reçoivent une pénalité légère ;
  - `include_superseded=true` permet de faire remonter les notes supersédées ou invalidées.

`search_regex` reste **littéral** : ni query-expansion, ni re-rank temporel. Utilise-le
quand tu cherches une chaîne exacte (un identifiant, un chemin, un bout de code).

Règle pratique : `search_text` pour « de quoi je parlais à propos de X », `search_regex`
pour « où ai-je écrit exactement cette chaîne ».

### Évaluer une mise à jour de connaissance

`datacron eval` mesure par défaut le vrai pipeline `search_text` (re-rank, confinement,
budget et snippets sérialisés). Pour tester qu'une ancienne information ne pollue plus les
réponses, crée une paire : la note active déclare l'ULID de l'ancienne dans `supersedes`,
puis ajoute une question au golden set privé :

```yaml
- id: rotation-certificats-active
  question: Quelle politique de rotation des certificats est active ?
  expected_paths:
    - securite/rotation-certificats-2026.md
  forbidden_paths:
    - securite/rotation-certificats-2025.md
```

Les chemins sont relatifs au vault et utilisent `/`. La question échoue en fraîcheur si la
note remplacée apparaît dans les cinq premières notes distinctes. Rejoue ensuite :

```bash
datacron eval --vault /chemin/vault --questions local/golden.yaml --save-baseline
datacron eval --vault /chemin/vault --questions local/golden.yaml --compare
```

Le second appel renvoie le code 1 si recall@5 ou nDCG@10 baisse de plus de 0,02. Ce seuil
se règle avec `DATACRON_EVAL_REGRESSION_TOLERANCE`.

## États de confiance des notes

Le frontmatter des notes porte des signaux que Datacron respecte au classement. Les plus
utiles au quotidien :

- `confidence: low` / `confidence: needs_verification` - la note est prise en compte mais
  légèrement démotée ; utile pour marquer un brouillon ou une info à vérifier.
- `supersedes: <ULID>` - désigne la note remplacée, qui sera fortement démotée dans les
  recherches courantes.
- `invalid_at: <datetime UTC>` avec `invalidated_by: <ULID>` - invalide un fait ciblé sans le
  supprimer ni réécrire son histoire.

Résultat : tu peux garder l'historique dans le vault sans polluer les réponses, tout en
pouvant le rappeler explicitement avec `include_superseded=true`.

### Consigner les options écartées

Utilise la liste frontmatter optionnelle `rejected` pour consigner les options écartées afin
qu'elles ne soient pas proposées de nouveau. Chaque entrée suit exactement le format
`option -- raison` ; une note peut contenir au maximum 16 entrées de 300 caractères chacune.
`create_note_ai` peut définir la liste et `set_frontmatter` la remplace entièrement ; passe
`rejected=[]` pour retirer la clé.

Ce champ est déclaratif dans cette version et ne modifie ni le retrieval ni
`contradiction_scan`. `list_notes` ne peut le filtrer que par correspondance insensible à la
casse sur une entrée complète.

### Cycle de vie d'un fait

Un fait est **actif** tant qu'il n'a ni été remplacé par une note qui le cite dans `supersedes`,
ni reçu de champ `invalid_at`. Quand une correction ciblée arrive, préfère
`invalid_at + invalidated_by` : la note devient **invalidée**, reste interrogeable comme historique
avec `include_superseded=true`, et la note de remplacement peut être écrite avant ou après elle.
`valid_from` précise si nécessaire la date de début de validité ; sinon `created` fait foi.

## Exemples de demandes concrètes

Tu formules en langage naturel ; Claude traduit en appels d'outils. Quelques exemples :

- « Qu'est-ce que j'ai noté sur la rotation des certificats ? »
  → `search_text` puis `get_note` sur les meilleurs résultats.
- « Montre-moi le plan de la note sur le déploiement entreprise. »
  → `get_note(format="map")`.
- « Où ai-je écrit exactement `DATACRON_WRITE_PATHS` ? »
  → `search_regex`.
- « Quelles notes renvoient vers celle sur la frontière de sécurité ? »
  → `get_backlinks`.
- « Ajoute une entrée de journal d'aujourd'hui sous "Suivi" dans la note projet Datacron. »
  → `append_journal` (nécessite l'écriture activée).
- « Passe cette note en confidence: low. »
  → `set_frontmatter`.
- « Est-ce que l'index est frais et intègre ? »
  → `get_health`.

## Bonnes pratiques

- **Garde un seul rédacteur** sur le vault : l'écriture concurrente multi-machines n'est pas
  supportée.
- **Laisse l'écriture désactivée** tant que tu n'en as pas besoin ; active-la sur un
  sous-dossier ciblé (`_memory`, par exemple), pas sur tout le vault.
- **Édite librement à la main** : Datacron répare l'index à la lecture, donc tes
  modifications manuelles sont prises en compte automatiquement.
- **Vérifie via `get_health`** après un gros changement plutôt que de deviner.
- **Range tes notes obsolètes** avec `supersedes` au lieu de les supprimer : tu gardes la
  traçabilité sans dégrader les recherches.

## Vie privée

Datacron ne fait pas de télémétrie et n'appelle aucun LLM cloud. En revanche, le client MCP
(par exemple Claude Desktop) peut, lui, transmettre à son fournisseur les chunks que
Datacron lui renvoie - Datacron ne lui envoie jamais le vault complet, seulement les
fragments pertinents et bornés. Détails : [Frontière de sécurité](security-boundary.md).

## Pour aller plus loin

- [Guide d'installation et de configuration](setup.md)
- [Architecture et surface publique](architecture.md)
- [Conventions du vault (SPEC)](spec.md)
- [Index de la documentation](index.md)
