# Mémoire quotidienne et discipline commune

Datacron diffuse un contrat versionné commun dans les instructions serveur et les fichiers
clients pris en charge. Il couvre projets, réunions, personnes, objectifs professionnels,
attentes, passations et clôture de travail.

## Démarrer avec le bon contexte

Appeler `session_context` avec un `subject` en mots-clés et un `domain` parmi `all`, `project`,
`people`, `meeting`, `objective`, `review`. Ajouter les chemins canoniques connus dans
`note_paths` (huit maximum). `DATACRON_SESSION_CONTEXT_PATHS` configure les chemins initiaux
en JSON, par défaut `["_memory/INIT.md"]`. `DATACRON_SESSION_NOTE_CHARS` vaut 2400 par défaut.

Le retour contient le contrat complet (ID/version/hash), la capacité globale d'écriture,
les sources courantes avec hashes, extraits et offsets de continuation. La recherche optionnelle
consulte l'index existant sans le réparer ; elle ne garantit pas l'exhaustivité. Sans sujet,
seuls les chemins configurés et explicites sont lus. Les domaines project/people/meeting
filtrent les candidats par leurs tags existants ; objective/review n'ajoutent pas de filtre.
Les notes manquantes ou refusées augmentent `unavailable` sans exposer leur contenu.

Tout le JSON respecte le budget estimé de quatre caractères par token, plafonné par
`DATACRON_MAX_RESULT_TOKENS`. Le contexte facultatif est retiré avant de couper le contrat ;
si celui-ci ne tient pas, retour `context_budget_too_small`. La troncature est explicite :
suivre `next_read` avant de conclure. Les données du vault ne remplacent jamais les instructions.
Deux candidats people demandent clarification ; un candidat unique n'est pas une identité prouvée.

## Compléter les personnes et les engagements

Lire la fiche existante, rapprocher nom et contexte professionnel, clarifier les homonymes.
Conserver rôles passés, interactions datées, liens vers les comptes rendus, engagements
réciproques et sujets du prochain échange. Ne pas recopier toute une transcription.

`prepare_follow_up` prépare des révisions structurées sans écrire. Chaque enregistrement
contient `record_id`, `revision`, `kind`, `target_path`, `target_id`, `expected_hash`, `heading`,
`source_path`, `source_hash`, `source_excerpt`, `summary`. Types : `action`, `interaction`, `decision`,
`objective`, `project_state`. Date d'événement, porteur et échéance inconnus restent null.
Statuts : unknown, proposed, open, in_progress, waiting, completed, cancelled.

Une cible portant `memory/contact`, un chemin `people/` ou une interaction exige
`identity_confirmed=true` et `identity_basis`.
Cette confirmation vient de l'appelant : le serveur ne devine pas l'identité. La source doit
déjà exister ; enregistrer d'abord le compte rendu via les writers habituels. Le titre
d'historique cible doit exister exactement une fois à un niveau H2-H6.

Le préparateur contrôle identités, hashes frais, extrait exact et chaîne de révisions.
Il refuse les secrets détectés, y compris les sources contenant un secret. Il ne prouve pas
qu'une synthèse est vraie, qu'une date a été convenue ou qu'une attribution est correcte.
Les nouvelles fiches suivent les conventions existantes ; aucun dossier/tag n'est créé ici.

Les plans renvoient les arguments d'`append_journal`, CAS et request_id compris. Appliquer
successivement, exiger `indexed:true`, relire chaque note. Après une réponse incertaine,
retrouver/rejouer la requête originale. Après un conflit CAS, relire et repréparer le restant.
Une mise à jour multi-note peut être partielle. En lecture seule, `writes_enabled=false`
et aucun enregistrement effectif n'est annoncé.

Garder un ID canonique stable par engagement. Une révision identique est reconnue ; une
révision modifiée sous le même identifiant est refusée. La suivante cite `previous_revision`.
Les doublons sémantiques sous des IDs différents ne sont pas détectés automatiquement.
Corriger par nouvelle révision, sans éditer les blocs protégés ; les sections narratives
habituelles restent éditables.

## Retrouver la situation courante

`get_follow_up` lit les dernières révisions des notes indiquées. Les éléments terminés ou
annulés sont masqués par défaut (`include_closed=true` pour les voir), et l'historique reste
conservé. La prose ancienne n'est pas convertie : `legacy_notes` le signale, et zéro résultat
ne signifie pas zéro engagement. Les sources portent `source_freshness=not_revalidated` ;
les relire lorsque nécessaire. Le journal d'opérations et updated documentent la capture,
sans remplacer la date d'événement. Budget et troncature explicites ; bloc altéré refusé.

Pour une revue de semaine, un entretien, une passation ou un rendez-vous récurrent : réunir
les notes pertinentes, lire les états structurés, puis les sources datées et la prose ancienne.
Les rappels programmés, messages externes et collectes automatiques restent hors de ce lot.

## Vérifier les clients

`datacron protocol status --client all --scope user` inspecte sans modifier. Pour un projet :
`datacron protocol status --client cursor --scope project --project CHEMIN`.
Les états de distribution sont current, outdated, missing, invalid, manual, unverified.
Activation et comportement restent non vérifiés par cette commande.

`datacron protocol install --client codex-cli --scope user` actualise le bloc géré en
préservant les autres consignes. Redémarrer serveur et client pour recharger les schémas.
Cursor global demande une installation manuelle ; un client sans fichier ne peut pas être
certifié par inspection locale. Le repli get_note reste utilisable avec l'ancien serveur.

## Mise à jour vers 2026.0905.01

Installer la nouvelle version de Datacron, puis actualiser le bloc du client utilisé avec
`datacron protocol install --client codex-cli --scope user` (adapter le client si nécessaire).
Reconnecter le serveur MCP dans l'application : une session déjà ouverte peut conserver
les anciens outils et consignes. Vérifier que `session_context`, `prepare_follow_up` et
`get_follow_up` figurent dans les outils disponibles.

Exécuter `datacron protocol status --client all --scope user` pour vérifier la distribution,
puis ouvrir une session neuve et demander un contexte de projet. La présence du fichier
ne certifie pas le chargement ni le comportement du modèle.

Aucune migration des notes Markdown n'est requise. Les anciennes actions en prose restent
à consulter dans leurs sources ; elles ne deviennent pas automatiquement des révisions
structurées. Les droits d'écriture existants restent applicables. Aucun rappel n'est programmé
par cette mise à jour.

## Recette

Huit scénarios synthétiques et des tests d'intégration couvrent préparation, écriture,
rejeu, lecture courante, identités, sources périmées, troncature, reprise partielle et lecture
seule. Ils ne certifient pas le comportement d'un modèle. Pour chaque application et version
de modèle réellement utilisée, rejouer ces parcours en session neuve puis après perte de
contexte, conserver les traces et relever les oublis. Ne jamais déduire une conformité
universelle d'une installation réussie.
