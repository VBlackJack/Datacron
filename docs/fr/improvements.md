# Écritures fiables et qualité de recherche

## Rejouer une écriture

Les huit outils ordinaires d'écriture acceptent `request_id`, facultatif : 1 à 128 caractères
ASCII parmi lettres, chiffres, points, tirets et underscores, avec une lettre ou un chiffre
en premier. Utiliser un identifiant unique par opération logique. Lors d'une nouvelle tentative,
conserver **tous les arguments**, y compris `expected_hash`. La clé est commune au vault,
entre outils et clients. Sa réutilisation avec un contenu ou une cible différente est refusée.

```json
{
  "rel_path": "_memory/exemple.md",
  "heading": "Journal",
  "entry": "Jalon vérifié.",
  "request_id": "jalon-20260905-001"
}
```

Le journal conserve les empreintes de la clé et des arguments, sans leur contenu brut.
La récupération et le contrôle de la clé se font sous le verrou interprocessus, avant le CAS
et avant la modification. Une transaction interrompue avant écriture peut être tentée à nouveau ;
une transaction déjà écrite et récupérée renvoie son reçu sans répéter la modification.

Le premier succès contient `operation_id`, `committed=true`, `replayed=false`, `rel_path`,
`content_hash`, `indexed=true` et le résultat habituel de l'outil. Le rejeu contient le reçu commun,
`replayed=true` et `indexed=false`. Il ne reconstruit pas le résultat propre à chaque outil.
Son hash décrit **l'écriture passée**, même si la note a été modifiée ou supprimée depuis.
Relire la note pour obtenir un hash utilisable dans une nouvelle opération CAS. Les contrôles
actuels de périmètre et d'autorisation d'écriture restent applicables.

Pour retrouver le reçu sans écrire :
`get_note_history(note="_memory/exemple.md", request_id="jalon-20260905-001")`.
L'absence de reçu ne prouve pas qu'une transaction en attente n'a rien écrit : effectuer la
récupération d'abord. Conserver le journal d'opérations est nécessaire à cette protection.
Les appels sans clé et les lots d'organisation conservent leur fonctionnement existant.
Redémarrer le serveur pour exposer les nouveaux schémas aux clients.

## Indexation ciblée

Une écriture ordinaire réindexe sa seule note cible et invalide le cache des alias.
`indexed=true` décrit cette note, pas la santé globale du vault. Une autre note malformée
ne perturbe plus l'acquittement. La vérification du hash refuse de confirmer l'indexation si
la cible a changé entre son écriture et sa relecture. Un échec sur la cible reste signalé par
`committed_index_incomplete` avec le hash écrit.

La réparation à la lecture, les commandes d'indexation et les contrôles de santé conservent
leur rôle global. Une indexation ciblée ne repousse pas l'échéance du prochain balayage global.

```text
uv run --frozen python scripts/benchmark_writes.py --sizes 100 1000 5000 --repeats 5
```

Cette mesure crée des vaults temporaires et produit un JSON avec les échantillons, médianes
et versions. Elle compare deux opérations distinctes ; elle ne promet pas un gain global
identique sur le vault de production.

## Sélection Markdown commune

Lecture et écriture utilisent la même identité de titre issue du parseur : formatage inline
retiré, fermeture ATX normalisée, titres Setext reconnus avec leur soulignement. Les titres
dans les blocs de code, citations et listes ne sont pas des sections racines adressables.
Les titres Setext multilignes gardent le texte concaténé utilisé par les chunks existants.
L'occurrence suit l'ordre du document après filtrage par niveau ; le CAS reste obligatoire
pour sélectionner un doublon. `append_journal` refuse désormais une sélection ambiguë.
Renommer ou supprimer H1 reste interdit. Les suffixes non touchés gardent leurs octets avec
des fins de ligne uniformes LF/CRLF et un éventuel BOM ; la règle existante reste valable
pour les fichiers à fins de ligne mixtes.

## Évaluation et livraison

Le corpus synthétique versionné `tests/fixtures/retrieval_quality/` contient 44 questions :
FR/EN, ambiguïtés, désambiguïsation, faits remplacés, chemins exclus et réponses absentes,
plus des cas durs : une note qui porte le nom du sujet face à des mentions en passant, un titre
de section jamais répété dans son corps, le même titre dans deux notes projet, des requêtes
bilingues sans expansion configurée, un index de backlog et une note d'archive qui répètent le
sujet, et une note `invalid_at` derrière sa remplaçante. Un cas de distracteur reste imparfait
tant que la démotion des archives n'existe pas ; il documente ce manque au lieu de le masquer.
`expected_empty: true` est incompatible avec des chemins ou chunks attendus. Son score
`empty_accuracy` est séparé du rappel, du MRR, du nDCG et de la précision des cas positifs.
Chaque résultat garde sa catégorie, sa latence et son coût en tokens. La comparaison à une
référence refuse les régressions ou la disparition des mesures de réponses absentes et de
chemins interdits lorsqu'elles étaient présentes dans la référence.

```text
uv run --frozen --extra dev pytest tests/integration/test_retrieval_quality.py
```

Ce test passe par la sérialisation publique MCP. Il ne mesure pas la décision d'un modèle
d'appeler un outil. La campagne petit modèle reste distincte. Les chiffres historiques du
README sur 19 questions ne sont pas les résultats de ce nouveau corpus.

Les publications réutilisent la CI complète : Python 3.11 à 3.13 sur Windows et Ubuntu,
couverture, invariants, dépendances et ShellCheck. `Quality gate` exige le succès de tous
ces jobs. Les règles du dépôt doivent exiger ce contrôle pour protéger réellement la branche ;
les fichiers de workflow seuls ne suffisent pas. Ces modifications ne déclenchent pas de release.

## Recherche ciblée et contexte des en-têtes

`search_text` accepte trois filtres de périmètre optionnels avec exactement la sémantique de
`list_notes` : `folder` (préfixe sur une frontière de dossier, confiné au vault), `tags` (chaque
tag listé doit être présent, comparaison insensible à la casse) et `frontmatter` (paires
clé/valeur de premier niveau, huit au plus, insensibles à la casse, une valeur liste correspond
sur n'importe quel élément). La réponse rappelle les filtres appliqués sous `filters` ; un appel
sans périmètre omet la clé. Le repli OR des requêtes à plusieurs termes s'exécute dans le même
périmètre : une recherche restreinte ne laisse jamais passer de résultat extérieur.

```json
{
  "query": "backlog",
  "folder": "_memory/projects",
  "tags": ["memory/project"],
  "frontmatter": {"confidence": "high"}
}
```

Chaque chunk indexé porte aussi une colonne `context` : le titre de la note, puis le chemin des
titres au-dessus du chunk, joints par ` / `. BM25 pondère cette colonne trois fois plus que le
corps du chunk (`SEARCH_CONTEXT_WEIGHT` et `SEARCH_CONTENT_WEIGHT`). Une note qui porte le nom
d'un sujet passe donc devant une note qui ne fait que le citer, et un titre de section reste
trouvable même quand son corps ne répète jamais ses mots. Les accents sont repliés des deux
côtés, comme pour le corps.

Une ouverture en écriture d'un index créé avant cette colonne renomme la table héritée, la
recrée avec la colonne et la remplit depuis ses propres lignes jointes aux titres indexés, dans
une seule transaction. Identités de chunks, hachés de contenu, ordinaux et plages de lignes sont
copiés tels quels : les références `chunk_id`, les hachés CAS et les projections de suivi
existants restent valides. Une ouverture certifiée en lecture seule ne migre jamais : elle
détecte la colonne absente et continue de servir un BM25 non pondéré jusqu'à ce que
`datacron reindex` reconstruise l'index.

Sur le corpus de recherche versionné (44 questions, pipeline outil), le changement fait passer
le rappel@5 par note de 0,972 à 1,0, le MRR de 0,921 à 0,972 et le nDCG@10 de 0,940 à 0,981,
avec une exactitude des réponses vides et un taux de violation des chemins interdits inchangés
à 1,0 et 0. Le test d'intégration échoue désormais sous un rappel@5 de 0,99, un MRR de 0,96 ou
un nDCG@10 de 0,97 : une régression de classement ne peut plus passer en silence.

`session_context` applique le même périmètre à sa recherche de sujet : quand un domaine
correspond à un tag mémoire, la liste bornée de candidats n'est construite qu'à partir des notes
qui portent ce tag, au lieu d'être remplie par des notes que le filtre de domaine écarterait
ensuite.
