# Écritures fiables et qualité de recherche

## Rejouer une écriture

Les huit outils ordinaires d’écriture acceptent `request_id`, facultatif : 1 à 128 caractères
ASCII parmi lettres, chiffres, points, tirets et underscores, avec une lettre ou un chiffre
en premier. Utiliser un identifiant unique par opération logique. Lors d’une nouvelle tentative,
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
`content_hash`, `indexed=true` et le résultat habituel de l’outil. Le rejeu contient le reçu commun,
`replayed=true` et `indexed=false`. Il ne reconstruit pas le résultat propre à chaque outil.
Son hash décrit **l’écriture passée**, même si la note a été modifiée ou supprimée depuis.
Relire la note pour obtenir un hash utilisable dans une nouvelle opération CAS. Les contrôles
actuels de périmètre et d’autorisation d’écriture restent applicables.

Pour retrouver le reçu sans écrire :
`get_note_history(note="_memory/exemple.md", request_id="jalon-20260905-001")`.
L’absence de reçu ne prouve pas qu’une transaction en attente n’a rien écrit : effectuer la
récupération d’abord. Conserver le journal d’opérations est nécessaire à cette protection.
Les appels sans clé et les lots d’organisation conservent leur fonctionnement existant.
Redémarrer le serveur pour exposer les nouveaux schémas aux clients.

## Indexation ciblée

Une écriture ordinaire réindexe sa seule note cible et invalide le cache des alias.
`indexed=true` décrit cette note, pas la santé globale du vault. Une autre note malformée
ne perturbe plus l’acquittement. La vérification du hash refuse de confirmer l’indexation si
la cible a changé entre son écriture et sa relecture. Un échec sur la cible reste signalé par
`committed_index_incomplete` avec le hash écrit.

La réparation à la lecture, les commandes d’indexation et les contrôles de santé conservent
leur rôle global. Une indexation ciblée ne repousse pas l’échéance du prochain balayage global.

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
L’occurrence suit l’ordre du document après filtrage par niveau ; le CAS reste obligatoire
pour sélectionner un doublon. `append_journal` refuse désormais une sélection ambiguë.
Renommer ou supprimer H1 reste interdit. Les suffixes non touchés gardent leurs octets avec
des fins de ligne uniformes LF/CRLF et un éventuel BOM ; la règle existante reste valable
pour les fichiers à fins de ligne mixtes.

## Évaluation et livraison

Le corpus synthétique versionné `tests/fixtures/retrieval_quality/` contient 32 questions :
FR/EN, ambiguïtés, désambiguïsation, faits remplacés, chemins exclus et réponses absentes.
`expected_empty: true` est incompatible avec des chemins ou chunks attendus. Son score
`empty_accuracy` est séparé du rappel, du MRR, du nDCG et de la précision des cas positifs.
Chaque résultat garde sa catégorie, sa latence et son coût en tokens. La comparaison à une
référence refuse les régressions ou la disparition des mesures de réponses absentes et de
chemins interdits lorsqu’elles étaient présentes dans la référence.

```text
uv run --frozen --extra dev pytest tests/integration/test_retrieval_quality.py
```

Ce test passe par la sérialisation publique MCP. Il ne mesure pas la décision d’un modèle
d’appeler un outil. La campagne petit modèle reste distincte. Les chiffres historiques du
README sur 19 questions ne sont pas les résultats de ce nouveau corpus.

Les publications réutilisent la CI complète : Python 3.11 à 3.13 sur Windows et Ubuntu,
couverture, invariants, dépendances et ShellCheck. `Quality gate` exige le succès de tous
ces jobs. Les règles du dépôt doivent exiger ce contrôle pour protéger réellement la branche ;
les fichiers de workflow seuls ne suffisent pas. Ces modifications ne déclenchent pas de release.
