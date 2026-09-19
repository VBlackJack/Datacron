# Parcours quotidiens et validation mesurée

[English](../en/daily-workflows.md) | **Français**

## Démarrer avec du contexte utile

`session_context` privilégie les `note_paths` explicites, puis les candidats du
sujet, puis les notes de démarrage configurées. Les filtres de domaine restent
appliqués aux candidats découverts. Le classement tient compte des signaux explicites
de cycle de vie. Cet outil ne répare pas l'index et indique les limites de couverture.

Le contrat mémoire complet reste la valeur par défaut. Un client qui possède encore
les instructions exactes peut transmettre leur `known_contract_hash`. Si le hash
correspond, la réponse conserve ID, version et hash avec `contract.delivery="unchanged"`.
Un hash absent ou différent reçoit les instructions complètes. Après perte de contexte,
il faut redemander le contrat complet. Cet accusé de cache ne prouve pas le respect
des instructions par le client.

Si le dernier extrait dépasse encore le budget, il est réduit en conservant le hash
actuel, l'encapsulation et la continuation exacte `next_read`. Les préférences de
sections peuvent alors céder la place à un extrait borné de la note entière.
`omitted` et `truncated` restent explicites ; la réponse ne promet pas l'exhaustivité.

## État actuel et historique

La recherche rétrograde les notes explicitement marquées `archived: true`,
`status: archived`, ou portant les anciens tags frontmatter `memory/archive` ou
`meta/archive`. Le chemin et la date de modification ne prouvent pas ce statut.
Les signaux d'invalidation et de remplacement gardent leur priorité. Les résultats
historiques exposent `lifecycle` : `archived`, `invalidated` ou `superseded`.
L'absence de ce champ ne certifie pas que l'information est actuelle.

Utilise `set_frontmatter(rel_path=..., archived=true, expected_hash=..., request_id=...)`
pour archiver via le writer durable. `archived=false` désactive ce champ ; un ancien
tag ou statut d'archive distinct doit également être résolu. Le hash actuel est
obligatoire pour cette décision. Aucun fichier n'est supprimé ou caché.
`include_superseded=true` désactive la rétrogradation en conservant les libellés ;
la lecture directe reste disponible. La politique des tags ne change pas : ne crée
pas de tag non déclaré pour archiver une note.

## Reprendre une mise à jour partielle

Conserve le `request_id`, la cible et les arguments de chaque écriture préparée.
`note` accepte le chemin relatif au vault ou l'ULID de la note, comme `get_note_history`
et `revert_note`. Appelle `get_write_progress` avec, par exemple :

```json
{"requests":[
  {"note":"project.md","request_id":"project-update-1"},
  {"note":"person.md","request_id":"person-update-1"}
]}
```

Tu peux ajouter le `expected_hash` d'origine de chaque opération. Chaque résultat
porte son `request_index` commençant à 1 et les compteurs résument le groupe.
L'outil reste en lecture seule ; il ne promet pas un instantané atomique multi-note.

| Statut | Signification et suite |
|---|---|
| `committed_current` | Le reçu correspond aux octets et à l'index actuels. Relire la note. |
| `committed_changed` | L'opération est enregistrée, puis les octets ont divergé. Relire sans répéter l'écriture. |
| `committed_reverted` | L'opération est enregistrée puis annulée : les octets courants sont ceux qu'elle avait remplacés. Rejouer les mêmes arguments avec `expected_hash`. |
| `committed_index_incomplete` | Les octets correspondent au reçu, mais pas l'index. Réparer l'index sans nouvelle mutation. |
| `conflict` | Aucun reçu trouvé et le hash CAS d'origine diffère. Examiner la requête initiale avant de repréparer. |
| `not_recorded` | Aucun reçu validé trouvé. Examiner la reprise ou rejouer exactement les mêmes arguments avec la même clé. |
| `target_unavailable` | La cible actuelle est illisible. Examiner avant de réessayer. |

Un reçu absent renvoie `committed=null` : cela ne prouve pas l'absence d'opération
pendante. Le hash du reçu est historique ; `current_hash` vient d'une lecture
ponctuelle. Toutes les références sont confinées avant de retourner le journal : une
référence qui n'est pas une note vivante admise, ou qui sort du vault, est refusée avec
l'erreur typée `note_not_admitted` avant toute inspection. Un groupe trop volumineux est
refusé et doit être divisé. Aucun reçu ne constitue une autorisation pour une nouvelle
écriture.

## Comprendre les diagnostics

`get_health` ajoute `guidance` : codes stables, gravité et actions pour un index
périmé, des identités incohérentes, une reprise bloquée ou des écritures désactivées.
Les blocages de reprise passent en premier. Les erreurs connues ajoutent `next_action` ;
les erreurs internes conservent leur identifiant de corrélation sans exposer les
chemins de la machine. L'état des autres clients n'est pas observable ici : compare
la version retournée avec le candidat installé et reconnecte après remplacement.

## Évaluer des conversations complètes

`tests/integration/test_daily_workflows.py` utilise de vrais processus MCP successifs
pour vérifier reconnexion, replay idempotent, réalisation partielle, conflits et
corrections. Les scénarios de discipline mémoire existants couvrent aussi réunions,
objectifs et homonymes. Ce sont des tests de protocole scénarisés ; ils ne mesurent
pas le comportement autonome d'un modèle.

`examples/conversations/` fournit six cas indépendants du fournisseur avec leurs
consignes. Exécute-les dans le client et le modèle évalués, puis exporte les échanges
d'outils en JSONL : `session`, `tool`, `arguments`, `result`. Une réponse finale
contient `session` et `answer`. Exemple d'évaluation :

```text
uv run --frozen python scripts/evaluate_conversation_trace.py --case examples/conversations/project-resume.json --trace local/trace.jsonl
```

Le vérificateur contrôle sessions distinctes, outils réussis requis, lectures des
sources citées, formulations attendues/interdites et relecture après écriture. Un
échec produit un code non nul. Adapte chemins et formulations à la fixture avant
la campagne, jamais après ses résultats. Une trace reste une preuve fournie par son
exporteur ; ces règles ne certifient pas la vérité sémantique. Aucun endpoint de
modèle n'est contacté automatiquement.

## Valider le fonctionnement réel

```text
uv run --frozen python scripts/benchmark_sessions.py --sizes 100 1000 5000 --clients 3 --repeats 20
```

Le script crée des vaults jetables et des processus MCP indépendants. Le JSON contient
version, machine, mesures brutes de démarrage/recherche/écriture, médiane, p95 par
rang et erreurs. Augmente les répétitions pour un essai prolongé. La première
recherche peut inclure une réparation ; elle n'est pas une mesure purement à chaud.
Avec peu de mesures, le p95 correspond souvent au maximum. Une erreur d'outil, un
résultat attendu absent ou une indexation non confirmée fait échouer la campagne.

La CI Windows exige de vrais liens symboliques de fichier et dossier avant la suite
complète. Le workflow manuel `Runtime validation` construit un candidat, l'installe
et le réinstalle sur une VM Windows hébergée jetable avec Python retiré du PATH
d'exécution, vérifie les hashes de configuration et de fixture, puis une connexion
MCP fraîche. Les benchmarks concurrents sont également conservés comme artefacts.

`scripts/verify_windows_install.py` exige simultanément `--allow-install` et
`DATACRON_DISPOSABLE_MACHINE=1` et refuse une installation ou une clé de registre
Datacron existante. Ne l'exécute jamais sur un poste de travail habituel. Une VM
hébergée ne représente pas une image Windows commerciale vierge : interface
interactive, accessibilité et démarrage sur image réellement vierge restent à
valider séparément. Ajouter le workflow ne prouve pas une exécution distante réussie.

Pour une image Windows Sandbox vierge, utilise l'installateur candidat et le
vérificateur autonome de l'artefact `windows-sandbox-inputs` du workflow :

```text
uv run --frozen python scripts/prepare_windows_sandbox.py --installer INPUT/Datacron-Setup.exe --validator INPUT/datacron-validation.exe --output local/sandbox-candidate
```

La destination doit être nouvelle. Ouvre `validate.wsb` sur un hôte disposant de
Windows Sandbox. Le réseau est désactivé, les exécutables sont partagés en lecture
seule et seul le dossier de résultats dédié est accessible en écriture. Contrôle
`results/exit-code.txt` (attendu : 0), `results/install-evidence.json` et le journal.
Le vérificateur est autonome : aucun Python installé dans le système invité n'est
nécessaire. Préparer le bundle ne lance aucune VM et ne prouve pas un essai réussi.
