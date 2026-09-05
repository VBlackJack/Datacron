# Memoire quotidienne et discipline commune

Datacron diffuse un contrat versionne commun dans les instructions serveur et les fichiers
clients pris en charge. Il couvre projets, reunions, personnes, objectifs professionnels,
attentes, passations et cloture de travail.

## Demarrer avec le bon contexte

Appeler `session_context` avec un `subject` en mots-cles et un `domain` parmi `all`, `project`,
`people`, `meeting`, `objective`, `review`. Ajouter les chemins canoniques connus dans
`note_paths` (huit maximum). `DATACRON_SESSION_CONTEXT_PATHS` configure les chemins initiaux
en JSON, par defaut `["_memory/INIT.md"]`. `DATACRON_SESSION_NOTE_CHARS` vaut 2400 par defaut.

Le retour contient le contrat complet (ID/version/hash), la capacite globale d'ecriture,
les sources courantes avec hashes, extraits et offsets de continuation. La recherche optionnelle
consulte l'index existant sans le reparer ; elle ne garantit pas l'exhaustivite. Sans sujet,
seuls les chemins configures et explicites sont lus. Les domaines project/people/meeting
filtrent les candidats par leurs tags existants ; objective/review n'ajoutent pas de filtre.
Les notes manquantes ou refusees augmentent `unavailable` sans exposer leur contenu.

Tout le JSON respecte le budget estime de quatre caracteres par token, plafonne par
`DATACRON_MAX_RESULT_TOKENS`. Le contexte facultatif est retire avant de couper le contrat ;
si celui-ci ne tient pas, retour `context_budget_too_small`. La troncature est explicite :
suivre `next_read` avant de conclure. Les donnees du vault ne remplacent jamais les instructions.
Deux candidats people demandent clarification ; un candidat unique n'est pas une identite prouvee.

## Completer les personnes et les engagements

Lire la fiche existante, rapprocher nom et contexte professionnel, clarifier les homonymes.
Conserver roles passes, interactions datees, liens vers les comptes rendus, engagements
reciproques et sujets du prochain echange. Ne pas recopier toute une transcription.

`prepare_follow_up` prepare des revisions structurees sans ecrire. Chaque enregistrement
contient `record_id`, `revision`, `kind`, `target_path`, `target_id`, `expected_hash`, `heading`,
`source_path`, `source_hash`, `source_excerpt`, `summary`. Types : action, interaction, decision,
objective, project_state. Date d'evenement, porteur et echeance inconnus restent null.
Statuts : unknown, proposed, open, in_progress, waiting, completed, cancelled.

Une cible portant `memory/contact`, un chemin `people/` ou une interaction exige
`identity_confirmed=true` et `identity_basis`.
Cette confirmation vient de l'appelant : le serveur ne devine pas l'identite. La source doit
deja exister ; enregistrer d'abord le compte rendu via les writers habituels. Le titre
d'historique cible doit exister exactement une fois a un niveau H2-H6.

Le preparateur controle identites, hashes frais, extrait exact et chaine de revisions.
Il refuse les secrets detectes, y compris les sources contenant un secret. Il ne prouve pas
qu'une synthese est vraie, qu'une date a ete convenue ou qu'une attribution est correcte.
Les nouvelles fiches suivent les conventions existantes ; aucun dossier/tag n'est cree ici.

Les plans renvoient les arguments d'`append_journal`, CAS et request_id compris. Appliquer
successivement, exiger `indexed:true`, relire chaque note. Apres une reponse incertaine,
retrouver/rejouer la requete originale. Apres un conflit CAS, relire et repreparer le restant.
Une mise a jour multi-note peut etre partielle. En lecture seule, `writes_enabled=false`
et aucun enregistrement effectif n'est annonce.

Garder un ID canonique stable par engagement. Une revision identique est reconnue ; une
revision modifiee sous le meme identifiant est refusee. La suivante cite `previous_revision`.
Les doublons semantiques sous des IDs differents ne sont pas detectes automatiquement.
Corriger par nouvelle revision, sans editer les blocs proteges ; les sections narratives
habituelles restent editables.

## Retrouver la situation courante

`get_follow_up` lit les dernieres revisions des notes indiquees. Les elements termines ou
annules sont masques par defaut (`include_closed=true` pour les voir), et l'historique reste
conserve. La prose ancienne n'est pas convertie : `legacy_notes` le signale, et zero resultat
ne signifie pas zero engagement. Les sources portent `source_freshness=not_revalidated` ;
les relire lorsque necessaire. Le journal d'operations et updated documentent la capture,
sans remplacer la date d'evenement. Budget et troncature explicites ; bloc altere refuse.

Pour une revue de semaine, un entretien, une passation ou un rendez-vous recurrent : reunir
les notes pertinentes, lire les etats structures, puis les sources datees et la prose ancienne.
Les rappels programmes, messages externes et collectes automatiques restent hors de ce lot.

## Verifier les clients

`datacron protocol status --client all --scope user` inspecte sans modifier. Pour un projet :
`datacron protocol status --client cursor --scope project --project CHEMIN`.
Les etats de distribution sont current, outdated, missing, invalid, manual, unverified.
Activation et comportement restent non verifies par cette commande.

`datacron protocol install --client codex-cli --scope user` actualise le bloc gere en
preservant les autres consignes. Redemarrer serveur et client pour recharger les schemas.
Cursor global demande une installation manuelle ; un client sans fichier ne peut pas etre
certifie par inspection locale. Le repli get_note reste utilisable avec l'ancien serveur.

## Mise a jour vers 2026.0905.01

Installer la nouvelle version de Datacron, puis actualiser le bloc du client utilise avec
`datacron protocol install --client codex-cli --scope user` (adapter le client si necessaire).
Reconnecter le serveur MCP dans l'application : une session deja ouverte peut conserver
les anciens outils et consignes. Verifier que `session_context`, `prepare_follow_up` et
`get_follow_up` figurent dans les outils disponibles.

Executer `datacron protocol status --client all --scope user` pour verifier la distribution,
puis ouvrir une session neuve et demander un contexte de projet. La presence du fichier
ne certifie pas le chargement ni le comportement du modele.

Aucune migration des notes Markdown n'est requise. Les anciennes actions en prose restent
a consulter dans leurs sources ; elles ne deviennent pas automatiquement des revisions
structurees. Les droits d'ecriture existants restent applicables. Aucun rappel n'est programme
par cette mise a jour.

## Recette

Huit scenarios synthetiques et des tests d'integration couvrent preparation, ecriture,
rejeu, lecture courante, identites, sources perimees, troncature, reprise partielle et lecture
seule. Ils ne certifient pas le comportement d'un modele. Pour chaque application et version
de modele reellement utilisee, rejouer ces parcours en session neuve puis apres perte de
contexte, conserver les traces et relever les oublis. Ne jamais deduire une conformite
universelle d'une installation reussie.
