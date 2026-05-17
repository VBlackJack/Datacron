# Architecture Local-First pour un Second Cerveau Obsidian en Lecture et Écriture

## Verdict technique

La bonne architecture n’est pas un simple “RAG sur un dossier Markdown”. Pour un Second Cerveau réellement **local-first**, **bidirectionnel** et **MCP-native**, il faut séparer clairement quatre couches : **le dépôt canonique** (vault Obsidian en Markdown/YAML), **la surface d’exposition MCP** (lecture + écriture outillée), **l’orchestrateur agentique** (routing, validation, HITL, reprise), et **les index locaux spécialisés** (lexical, regex, vectoriel, graph/light metadata). Obsidian reste un bon centre de gravité parce qu’il stocke les notes comme des fichiers Markdown en local et rafraîchit automatiquement le vault quand des changements externes arrivent sur disque. MCP, de son côté, est bien conçu pour connecter un agent à des outils, des ressources et des workflows via `stdio` ou Streamable HTTP. citeturn20search7turn36view0turn36view1turn36view2turn36view3

Si l’objectif est **consultation + écriture sûre**, ma recommandation de référence est la suivante : **Obsidian vault comme source de vérité**, **serveur MCP custom en Python via FastMCP** pour exposer des outils de lecture/écriture minimaux et gouvernés, **LangGraph** comme runtime principal si vous voulez des workflows robustes avec interruption humaine et reprise, et **LanceDB + SQLite FTS5 + ripgrep** comme triptyque de recherche locale. LanceDB est aujourd’hui le choix le plus cohérent si vous voulez vectoriel + BM25 + hybride + filtres + mises à jour dans une seule brique locale. SQLite FTS5 reste excellent pour l’index de routage rapide. ripgrep reste la meilleure “voie d’urgence” pour les requêtes exactes, les symboles, les clés YAML, les erreurs de logs et les regex. citeturn9view1turn33view3turn33view0turn33view1turn33view2turn15search6turn14search9turn15search0turn15search10turn17search0turn17search2turn17search1turn17search3

Le point le plus important est le suivant : **ne laissez jamais le modèle écrire directement dans votre couche canonique “knowledge” sans contrat d’écriture**. MCP permet l’action, mais la spécification insiste aussi sur le fait qu’une application devrait laisser un humain refuser les invocations d’outils, et les mécanismes modernes d’orchestration savent interrompre, persister l’état et reprendre ensuite. Votre write path doit donc être conçu comme une **transaction gouvernée**, pas comme un simple `write_file`. citeturn36view2turn33view0turn33view1turn33view2

## Écosystème open source pertinent

### Cadre de choix

La meilleure manière de penser l’écosystème actuel est : **MCP pour l’interopérabilité**, **un orchestrateur pour décider**, **un ou plusieurs serveurs MCP pour agir**, et **des index locaux pour retrouver**. Le dépôt officiel `modelcontextprotocol/servers` est utile pour comprendre les patterns, mais le README officiel rappelle explicitement que ces serveurs sont des **références pédagogiques**, pas des solutions prêtes pour la production. Il faut donc souvent construire ou durcir votre propre surface MCP. citeturn22view0

### Frameworks et runtimes à privilégier

| Brique | Ce que la documentation confirme | Verdict pour votre cas |
| --- | --- | --- |
| **LangGraph** | Exécution durable, persistence par checkpoints, human-in-the-loop, interrupt/resume, routing et arêtes conditionnelles. citeturn33view3turn33view0turn33view1turn33view2turn28view0 | **Meilleur choix** pour piloter un agent read/write avec états, validations et reprise après review. |
| **LangChain + adapters MCP** | `MultiServerMCPClient`, chargement d’outils MCP, sessions explicites si l’on veut du stateful, intégration native avec agents. citeturn35view0 | Très bon si vous êtes déjà dans l’écosystème LangChain, mais je privilégierais **LangGraph** pour la boucle bidirectionnelle. |
| **PydanticAI** | Capacité MCP, recherche différée d’outils (`ToolSearch`), hooks avant/après validation/exécution d’outils, instrumentation OTel. citeturn9view0 | **Excellent choix léger** pour un agent local-first très contrôlé, surtout si vous voulez une gouvernance tool-centric. |
| **LlamaIndex** | Consomme des serveurs MCP, convertit workflows/outils en serveurs MCP, dispose de routeurs, BM25 retriever, parsing et chunking avancés. citeturn35view1turn35view2turn28view2turn28view3turn28view4turn29view0 | **Très fort côté documents et retrieval**. Parfait si votre priorité est la couche RAG/document engineering. |
| **AutoGen** | Runtime événementiel/distribué, `McpWorkbench`, intervention handlers pour approbation des tools, patterns multi-agents. citeturn34search8turn34search9turn34search1turn34search12 | Solide si vous voulez plusieurs agents spécialisés et un bus événements, moins simple à tenir qu’un LangGraph sobre. |
| **FastMCP** | Construction rapide de serveurs MCP, génération des schémas d’outils, clients MCP, gestion du contexte, progress, user elicitation. citeturn6search1turn9view1turn6search10 | **Brique serveur recommandée** pour exposer vos outils de lecture/écriture et vos ressources d’index. |

### Serveurs MCP côté Obsidian et Markdown local

Il existe aujourd’hui deux familles réalistes.

La première est la famille **plugin/API Obsidian**. Le plugin **Obsidian Local REST API** expose un CRUD complet sur le vault, la lecture/écriture du fichier actif, les notes périodiques, la recherche simple ou JSONLogic, et surtout le **patch ciblé par heading, block reference ou clé frontmatter**, avec API key et HTTPS auto-signé. Un wrapper MCP comme `cyanheads/obsidian-mcp-server` ajoute des outils typés, des permissions lecture/écriture par dossier, un mode lecture seule, un `ctx.elicit` pour confirmer certains deletes, et des opérations de réconciliation de tags/frontmatter. Si vous avez besoin d’interagir avec l’UI Obsidian, d’ouvrir une note, ou de profiter de sémantiques “Obsidian aware” sans réimplémenter tout, c’est aujourd’hui la voie la plus pragmatique. citeturn25view0turn25view1

La seconde est la famille **filesystem natif**. Des serveurs communautaires récents comme `lstpsche/obsidian-mcp` lisent directement le vault sans plugin ni application Obsidian en cours d’exécution, comprennent le format Obsidian, construisent des index mémoire synchronisés par watcher filesystem, et exposent lecture/écriture/recherche BM25/regex/embeddings. D’autres projets comme **TurboVault** vont encore plus loin : parsing OFM natif, graph des liens, batch atomique, requêtes SQL frontmatter, détection de conflits par hash, atomic writes, profils read-only/production. Pour un système “daemon local” autonome, c’est la famille la plus intéressante. citeturn25view2turn26view0

### Compatibilité avec les runtimes LLM

L’écosystème local n’est pas homogène. **LM Studio** documente explicitement l’usage de serveurs MCP via son API, soit en éphémère par requête, soit via `mcp.json`, ce qui en fait aujourd’hui la meilleure option si vous voulez un runtime local qui parle MCP “de façon visible” dans la doc. **Ollama** documente fortement le tool calling, y compris le streaming des tool calls, et mentionne que ces améliorations servent aussi l’usage avec MCP. **vLLM** documente le tool calling, les structured outputs et des API compatibles OpenAI/Anthropic, mais pas dans la doc consultée comme un client MCP de premier rang. En pratique, pour **Ollama** et **vLLM**, le design le plus sûr reste donc de **terminer MCP dans l’orchestrateur** puis d’envoyer au runtime des tool calls ordinaires. citeturn37view1turn37view2turn37view3turn9view0

## Pipeline d’écriture bidirectionnel

### Principe de base

Le write pipeline doit être traité comme une **boucle transactionnelle avec état**, pas comme un append opportuniste. MCP fournit exactement les briques nécessaires : des **tools** pour agir, des **resources** pour exposer le contexte ou les manifests d’index, et **elicitation** pour demander un input structuré au client humain si nécessaire. LangGraph fournit le runtime idéal pour suspendre, faire relire et reprendre plus tard avec persistance. citeturn36view2turn36view3turn36view5turn33view0turn33view1turn33view2

### Write path recommandé

Je recommande une boucle en sept étapes.

D’abord, l’agent classe l’intention en **lecture pure**, **mise à jour ciblée**, **création**, **journalisation**, ou **opération destructive**. Cette classification peut être faite par un router dédié dans LangGraph ou LangChain, ou par un sélecteur/routeur LlamaIndex, mais pour les écritures il faut garder une couche déterministe et explicite devant le LLM. Les docs LangChain et LlamaIndex confirment bien l’existence de routeurs dédiés, par classification simple ou agrégation multi-sources. citeturn28view0turn28view1turn28view2turn28view3

Ensuite, l’agent ne doit **jamais** écrire tout de suite dans la note cible principale. Il doit produire un **plan de changement** contenant au minimum : cible pressentie, type d’opération, preuves récupérées, diff proposé, niveau de risque, et besoin éventuel de review. C’est exactement le genre d’objet que LangGraph sait garder en checkpoint et que PydanticAI sait contrôler via hooks avant validation/exécution d’outils. citeturn33view2turn9view0

Troisième étape : le changement est écrit dans un **tampon local**, typiquement un dossier `_inbox/ai/`, `_staging/patches/`, ou `_journal/agent/`. Les écritures “sans risque” peuvent être auto-appliquées dans des namespaces dédiés : journaux de bord, logs d’analyse, synthèses temporaires, captures de réunion. Les écritures “sur savoir canonique” doivent passer par `needs_review`, sauf si vous avez une règle d’auto-merge extrêmement conservatrice. La spécification MCP recommande de garder l’humain capable de refuser les invocations d’outils. LangChain, AutoGen et MCP/elicitation savent tous formaliser cette étape. citeturn36view2turn36view5turn33view0turn34search1turn34search0

Quatrième étape : si la cible est une note existante, faites une **écriture chirurgicale**, pas un remplacement global. La Local REST API d’Obsidian expose précisément ce pattern par `PATCH` sur heading, block reference ou frontmatter key. C’est très important pour éviter d’écraser des zones non concernées, surtout dans des notes longues avec tables, scripts et liens wiki. citeturn25view0

Cinquième étape : avant l’application finale, vérifiez une **précondition de fraîcheur**. Ma recommandation est un `content_hash_before` calculé par note ou par section cible. Si le hash ne correspond plus, le pipeline ne remplace rien : il génère un conflit et renvoie vers review manuelle ou fusion. Des serveurs filesystem récents comme TurboVault documentent déjà ce type d’idée avec atomic writes et conflict detection par hash. citeturn26view0

Sixième étape : après application, mettez à jour **de façon synchrone** l’index lexical léger et le manifeste de ressources, puis lancez **de façon asynchrone** la réindexation vectorielle. SQLite FTS5 accepte des `INSERT`, `UPDATE` et `DELETE` comme une table ordinaire, ce qui se prête très bien à une mise à jour immédiate de l’index de routage. La vectorisation, plus coûteuse, peut être poussée dans une file locale. Les checklists LlamaIndex insistent précisément sur les risques de fragmentation d’index et recommandent le suivi de `doc_id`, le refresh des documents modifiés, la déduplication et parfois des rebuilds périodiques. citeturn17search0turn29view1turn15search10turn14search16

Septième étape : journalisez tout dans un **audit log append-only** local. Ce log doit être indépendant de la note finale et contenir `run_id`, horodatage, modèle, retrievers utilisés, chunks effectivement cités, outils appelés et décision humaine. Les frameworks actuels facilitent l’instrumentation : LangGraph via sa persistance et PydanticAI via OpenTelemetry/Logfire hooks. citeturn33view2turn9view0

Un frontmatter utile pour vos brouillons et patches pourrait ressembler à ceci :

```yaml
ai:
  status: needs_review
  operation: patch_existing
  target_path: "Architecture/RAG.md"
  target_selector:
    type: heading
    value: "Stratégie de chunking"
  run_id: "2026-05-17T14-32-06Z_9f4d"
  model: "local/qwen3"
  evidence:
    - doc_id: "conf_export_123"
      chunk_id: "conf_export_123::H2/RAG::0007"
  content_hash_before: "sha256:..."
  created_at: "2026-05-17T14:32:06+02:00"
```

### Deux implémentations viables

Si vous voulez **aller vite**, partez sur **Obsidian Local REST API + wrapper MCP existant**, parce que vous récupérez immédiatement le patch ciblé, l’ouverture de note dans l’UI, les commandes Obsidian et une sémantique déjà prête pour frontmatter/tags. citeturn25view0turn25view1

Si vous voulez **robustesse maximale et zéro dépendance à l’application Obsidian**, partez sur un **serveur MCP filesystem natif** ou développez le vôtre avec FastMCP. C’est la meilleure option si vous voulez exécuter l’agent même quand Obsidian est fermé, garder un watcher local, et maîtriser totalement les contraintes d’écriture, les verrous et la journalisation. citeturn25view2turn26view0turn9view1

## Chunking et indexation pour Markdown technique

### Ce qu’il ne faut pas faire

Évitez le chunking purement “taille fixe par caractères” sur du Markdown technique. Les docs LlamaIndex le disent de façon assez claire : les mauvaises réponses viennent souvent d’un **mauvais découpage** qui sépare le contexte critique, et ils recommandent explicitement des chunk sizes ajustés, des splitters fondés sur phrases, du hiérarchique et du sentence-window. citeturn29view1turn30view0turn30view1

### Politique de chunking recommandée

Pour votre corpus Confluence exporté vers Markdown, la meilleure stratégie est un **chunking structurel en plusieurs passes**.

La première passe doit être **syntaxique** : frontmatter YAML, chemin de headers, listes, tableaux, blocs de code, tâches, wikilinks et block refs. LlamaIndex expose un `MarkdownNodeParser` qui conserve le chemin de headers en metadata, un `SimpleFileNodeParser` pour choisir le bon parser selon le type de fichier, et un `CodeSplitter` pour les blocs de code par langage. LangChain apporte aussi `MarkdownHeaderTextSplitter`, qui garde les headers en metadata, et un splitter de code/langage très pratique. citeturn29view2turn29view3turn32view0turn32view1

La deuxième passe doit être **hiérarchique**. Le pattern “small-to-big” est aujourd’hui le plus intéressant pour votre cas : un parent large pour conserver la cohérence d’une grande section, et des enfants plus petits pour pointer précisément une information. LlamaIndex documente ce pattern via `HierarchicalNodeParser` et `AutoMergingRetriever`, avec une hiérarchie coarse-to-fine et un leaf-level indexé séparément. citeturn29view0

La troisième passe doit être **spécialisée selon le contenu**. Pour des paragraphes denses, un `SemanticSplitterNodeParser` ou un split par phrases avec fenêtre de contexte fonctionne mieux qu’un découpage brut. Pour des snippets techniques, utilisez un code splitter dédié. Pour des tableaux, ne les mélangez pas avec du texte narratif : Unstructured documente explicitement le traitement séparé des `Table` et `TableChunk`, ainsi que la conservation des éléments d’origine via `orig_elements`. C’est très utile si un agent doit ensuite **mettre à jour précisément** une cellule, une ligne logique ou une section contenant un tableau. citeturn30view1turn30view0turn31view1turn31view0

Concrètement, je recommande ce schéma de métadonnées de chunk :

- `doc_id` stable
- `doc_version`
- `path`
- `header_path`
- `section_title`
- `chunk_type` (`frontmatter`, `narrative`, `code`, `table`, `list`, `quote`)
- `lang` pour les blocs de code
- `wikilinks_out[]`
- `backlink_candidates[]`
- `source_export` / `space_key` / `confluence_page_id`
- `token_count`
- `content_hash`
- `parent_chunk_id` / `prev_chunk_id` / `next_chunk_id`

Ce schéma n’est pas imposé par les outils, mais il s’aligne très bien avec leurs capacités de parsing, de relations `prev/next`, de hiérarchie et de récupération ciblée. citeturn29view2turn29view0turn30view0

### Choix d’index local

Pour l’index vectoriel/hybride, le comparatif réaliste est le suivant.

| Moteur | Ce que la doc confirme | Lecture pour votre cas |
| --- | --- | --- |
| **LanceDB** | Supporte vector search, full-text BM25, hybrid search avec reranking, filtres, SQL, `update` et `merge_insert`. citeturn15search6turn14search9turn15search0turn15search10turn15search11 | **Meilleur fit local-first** si vous voulez un moteur unique pour dense+sparse. |
| **Chroma** | `PersistentClient` local, query dense, filtres metadata, `where_document` avec contiens/regex, update/delete; mais le nouveau Search API hybride documenté est indiqué comme disponible sur Chroma Cloud et non encore sur single-node. citeturn14search6turn16search5turn16search2turn16search7turn14search16turn14search20 | Très bon pour local dense + filtres, moins cohérent si vous voulez du **hybride local unifié** immédiatement. |
| **FAISS** | Bibliothèque de similarité/clustering efficaces sur vecteurs denses, potentiellement à très grande échelle. citeturn14search2 | Excellent comme noyau dense, mais il faut composer vous-même lexical, filtres et gouvernance. |

Mon conseil concret est simple : **LanceDB** comme moteur hybride principal si vous voulez simplifier, ou **FAISS + SQLite FTS5** si vous voulez un assemblage très explicite et minimaliste. Chroma reste très valable si vous êtes déjà à l’aise avec lui, mais il est aujourd’hui moins net pour un mode “tout local hybride dans une seule API” que LanceDB. citeturn15search6turn15search0turn14search6turn16search7turn14search2turn17search0

## Routage dynamique de l’agent

### Règle générale

Le meilleur routeur pour une base Obsidian technique n’est pas “un LLM qui choisit tout seul”. Le meilleur routeur est un **front-router déterministe** suivi d’un **router LLM secondaire** quand la requête est ambiguë. Les frameworks actuels savent router dynamiquement, mais pour les écritures il faut réduire la liberté du modèle. LangChain documente un router dédié de pré-classification, et LlamaIndex documente des routeurs sélectionnant un ou plusieurs retrievers/query engines selon la requête. citeturn28view0turn28view2turn28view3

### Politique de décision recommandée

Si la requête contient un **nom de fichier, un header, une clé YAML, un tag, un symbole de code, une stacktrace, une regex, une date précise ou une mention quasi exacte**, commencez par **ripgrep + SQLite FTS5/BM25**. ripgrep est très rapide, respecte `.gitignore`, saute les fichiers cachés et binaires par défaut, et SQLite FTS5 apporte un vrai full-text indexé avec BM25. citeturn17search1turn17search3turn17search0turn17search2

Si la requête est une **paraphrase, une question conceptuelle, une reformulation métier ou une recherche transversale**, utilisez **hybride dense+sparse**. LanceDB documente explicitement ce pattern, et Chroma documente dense+sparse/hybrid dans son offre, tout en rappelant que le nouveau Search API hybride est Cloud-only pour le moment. citeturn15search0turn15search6turn16search3turn16search7

Si le but est une **mise à jour**, ne laissez pas le routeur décider “create vs update” uniquement sur le langage naturel. Faites d’abord une **résolution de cible** : titre exact, alias, wikilink, tags, voisinage vectoriel, header_path, et éventuellement graphe de liens. Les serveurs Obsidian spécialisés commencent justement à intégrer cette logique de path resolution, backlinks et graph awareness. citeturn25view1turn25view2turn26view0

Je conseille la logique suivante :

- **Update note existante** si vous avez un candidat fort et stable : path exact, alias exact, ou score hybride élevé avec couverture d’évidence suffisante.
- **Patch de section** si le besoin est localisé à un heading, bloc ou frontmatter.
- **Create note nouvelle** uniquement si aucun candidat n’atteint le seuil, après un anti-duplication sur titres, aliases et top-k vectoriels.
- **Créer en Inbox plutôt qu’en dossier canonique** si la confiance est moyenne.
- **Refuser d’écrire** si la couverture d’évidence est insuffisante ou contradictoire.

Pour les grands ensembles d’outils MCP, **PydanticAI** est particulièrement intéressant parce qu’il expose une vraie `ToolSearch` avec chargement différé et stratégies de recherche locales ou natives selon le provider. Cela aide à empêcher qu’un modèle voie d’emblée tout votre arsenal de tools d’écriture. citeturn9view0

## Limites, conflits et garde-fous

### Hallucinations documentaires

Le risque principal n’est pas seulement l’hallucination de réponse, mais l’**hallucination de modification** : l’agent écrit une synthèse ou “corrige” une note sur la base d’un contexte partiel. Les checklists LlamaIndex listent exactement ce type de problème via le mauvais chunking, la fragmentation d’index et les versions contradictoires. La parade est simple : exigez que chaque write action cite des chunks source, stockez `doc_id`/`doc_version`, rafraîchissez les documents modifiés et reconstruisez périodiquement plutôt que d’empiler indéfiniment. citeturn29view1

### Écrasement et corruption de données

Le second risque est l’écrasement silencieux d’une note vivante. La réponse n’est pas “interdire l’écriture”, mais **contraindre le mode d’écriture** : patch ciblé quand possible, atomic write, précondition hash avant merge, et dossier tampon pour review. Le plugin Local REST API expose un patch ciblé, tandis que des serveurs filesystem plus récents documentent atomic writes et conflict detection par hash. citeturn25view0turn26view0

### Boucles infinies de création ou d’auto-maintenance

Un agent bidirectionnel peut se mettre à créer des notes de synthèse sur ses propres sorties, puis à les réindexer, puis à les relire comme sources. Il faut imposer trois garde-fous : **budget d’écriture par run**, **namespace séparé pour les écrits IA**, et **politique de lecture qui dépriorise ou exclut les notes `ai.status != approved`**. MCP, LangGraph et AutoGen fournissent les primitives HITL/intervention pour stopper ce type de boucle avant exécution. citeturn36view2turn33view0turn33view1turn34search1

### Latence de réindexation

La latence est inévitable si vous vectorisez en local. Le bon compromis est **lexical d’abord, vectoriel ensuite**. Une note vient d’être écrite : elle doit devenir immédiatement retrouvable par path, heading, tag, mot-clé ou regex, puis rejoindre l’index sémantique dans un second temps. SQLite FTS5 et les moteurs locaux documentent tous des opérations de mise à jour, beaucoup plus légères qu’une ré-embedding complète. citeturn17search0turn15search10turn14search16

### Surface d’attaque MCP

Si vous exposez votre serveur MCP en HTTP, la spécification demande explicitement la validation de l’`Origin`, recommande un bind sur `127.0.0.1` et une authentification correcte. La doc d’autorisation MCP recommande aussi OAuth 2.1 pour les serveurs distants, tout en rappelant que pour `stdio` local on peut rester sur des secrets/environnements locaux. Votre posture par défaut doit donc être : **`stdio` en local**, **HTTP seulement si nécessaire**, **localhost uniquement**, **auth obligatoire**, et **write tools séparés du read surface**. citeturn36view1turn36view4

### Maturité des serveurs existants

Le dernier risque est organisationnel : croire qu’un serveur MCP open source “qui marche en demo” est déjà adapté à votre dépôt documentaire critique. La documentation officielle du dépôt de serveurs MCP est explicite : ce sont des **serveurs de référence**, pas des solutions prêtes production. Pour votre cas, cela plaide très fortement pour une **surface MCP maison, minimale et durcie**, même si vous réutilisez des wrappers existants pour accélérer le prototypage. citeturn22view0

## Préconisation d’infrastructure logicielle

Le montage que je recommanderais en priorité est celui-ci.

**Couche canonique** : un vault Obsidian classique, organisé en répertoires fonctionnels, avec `doc_id`, `doc_version`, `aliases`, `tags`, `source_system`, `hash` dans le frontmatter. Obsidian garde l’avantage du stockage Markdown local et du refresh automatique des modifications externes. citeturn20search7

**Serveur MCP** : un serveur custom **FastMCP** exposant des tools minimaux : `list_notes`, `get_note`, `search_lexical`, `search_regex`, `search_vector`, `patch_note_section`, `create_note_in_inbox`, `append_journal`, `update_frontmatter`, `request_review`, `approve_patch`, `reindex_doc`. Exposez aussi des **resources** pour le manifeste des index, les write policies et l’état de la queue de réindexation. FastMCP est bien adapté pour cela, et MCP permet officiellement tools + resources + elicitation. citeturn9view1turn6search10turn36view2turn36view3turn36view5

**Orchestration** : **LangGraph** si vous voulez une machine à états propre et un vrai cycle suspendre/reprendre/reviewer. **PydanticAI** si vous voulez une version plus légère mais très contrôlée, orientée policies sur les tools. **LlamaIndex** vient ensuite comme excellent sous-système pour parsing/chunking/retrieval, pas forcément comme runtime unique de gouvernance read/write. citeturn33view3turn33view0turn33view1turn33view2turn9view0turn35view1turn35view2

**Recherche** : **LanceDB** comme moteur hybride principal, **SQLite FTS5** comme index de routage/manifest très bon marché, **ripgrep** comme fallback exact/regex. Si vous préférez une pile plus modulaire, FAISS + FTS5 fonctionne très bien, mais il faut tout assembler vous-même. citeturn15search6turn15search0turn14search9turn17search0turn17search1turn14search2

**Runtimes modèles** : si vous voulez du MCP directement côté runtime, **LM Studio** est aujourd’hui le plus explicite dans sa doc. Pour **Ollama** et **vLLM**, je les traiterais comme des moteurs de génération/tool calling branchés derrière votre orchestrateur MCP. citeturn37view1turn37view2turn37view3

## Questions ouvertes et limites

Il reste quelques décisions qui dépendent fortement de votre corpus réel.

La première est le **choix plugin Obsidian vs filesystem natif**. Si vous avez besoin d’ouvrir des notes dans l’UI, de tirer parti des commandes Obsidian et d’un patch déjà “aware” des headings/frontmatter, la voie Local REST API est plus simple. Si votre priorité est l’autonomie hors UI, la robustesse démon et un contrôle total de l’I/O, la voie filesystem natif est meilleure. citeturn25view0turn25view1turn25view2turn26view0

La deuxième est le **benchmark retrieval** sur votre corpus exact. Les docs confirment les capacités des moteurs et des splitters, mais elles ne remplacent pas un test sur vos exports Confluence nettoyés, notamment pour les pages riches en tableaux, macros transformées, scripts et block refs. Les paramètres de chunking, la pondération BM25/vector et les seuils create-vs-update devront être calibrés empiriquement. citeturn29view1turn31view1turn15search0turn15search11

La troisième est la **stratégie d’embeddings locale**. Les docs consultées confirment les capacités de stockage, de routage et d’orchestration, mais pas un benchmark unique d’embedder local optimal pour votre type de Markdown technique. Il faut donc considérer ce point comme **à valider expérimentalement**, indépendamment du design global.