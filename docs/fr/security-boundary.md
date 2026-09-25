---
title: Frontière de sécurité locale de Datacron
verified: 2026-09-20
tested_on: "Datacron MCP stdio / mcp 2.0.0 / Python 3.11.15"
---

# Frontière de sécurité locale de Datacron

**Français** | [English](../en/security-boundary.md)

Datacron sert aujourd'hui un seul vault Markdown local via MCP stdio. La garantie de sécurité
du serveur est délibérément plus étroite qu'une résistance à l'injection de prompt pour un
agent ou un modèle.

## Frontière de responsabilité

Le serveur garantit que :

- l'accès filesystem aux notes passe par un unique `VaultScope` ;
- les lectures restent dans le vault configuré et les écritures exigent en plus
  `DATACRON_WRITE_PATHS` ;
- le texte du vault est renvoyé dans une enveloppe de données seule ;
- les outils enregistrés n'évaluent pas le contenu des notes et ne le transforment pas en
  commande filesystem, processus arbitraire ou requête réseau ;
- l'attribution d'écriture vient du contexte de transport MCP, jamais du contenu d'une note ;
- les valeurs probablement secrètes sont expurgées aux frontières de sortie configurées.

Le consommateur MCP reste responsable de décider s'il appelle un autre outil. Un modèle peut
toujours être influencé par une prose hostile et recopier cette prose dans un nouvel appel
d'outil explicite. L'enveloppe et l'échappement côté serveur ne prouvent pas la conformité du
modèle.

## Identité de l'appelant

Le transport supporté est le stdio local. `StdioCallerIdentityProvider` est le seul point
d'attribution de l'appelant. La connexion du processus OS local est la frontière de confiance ;
le nom du client MCP, sa version et son identifiant client sont des métadonnées d'attribution
auto-déclarées, pas des identifiants vérifiés cryptographiquement. Le contenu du vault ne peut
pas fixer l'acteur dans le journal d'opérations durable.

Un transport distant doit remplacer le fournisseur d'identité par un fournisseur qui valide
les identifiants avant de construire un acteur. L'authentification distante, le SSO, les
espaces de noms par tenant et les ACL inter-tenants ne sont pas implémentés.

## Périmètre du vault

`SingleTenantVaultScope` confine tout chemin à une racine de vault configurée, et les deux
frontières la rétrécissent différemment.

Les lectures ne sont **pas** autorisées dans tout le vault. Toute lecture de note passe aussi
l'admission : le chemin doit finir par `.md`, aucun composant parent ne doit commencer par un
point ni figurer dans `excluded_folders`, et le nom de fichier ne doit pas figurer dans
`excluded_files`, les deux venant de `VAULT.yaml` et comparés sans tenir compte de la casse. Un
fichier que le vault contient mais que la politique exclut est refusé à tous les outils de
lecture. Chaque entrée de `excluded_folders` et `excluded_files` est un nom unique, comparé à
un composant de chemin n'importe où dans le vault : un `/` final est retiré, et une entrée qui
contient encore un `/` ou un `\` est refusée au démarrage, en la nommant, au lieu d'être
chargée comme une exclusion qui ne correspondrait jamais à rien. Sous Windows, un composant de
chemin contenant `:` est refusé, car il désigne un flux de données alternatif NTFS.

Les écritures sont confinées à la racine du vault et doivent en plus tomber dans une racine
`DATACRON_WRITE_PATHS` explicite. Aucune des deux frontières n'implique l'autre : un chemin peut
être lisible et non inscriptible, et un chemin hors admission n'est ni l'un ni l'autre. Toute
écriture de note passe la même admission qu'une lecture, sur le chemin tel que donné et sur le
chemin résolu, avant que la note soit ouverte, et le refus est identique que la note existe ou
non. Une écriture refuse aussi un chemin qui traverse un lien symbolique ou une jonction, même
quand il reste dans le vault. Un chemin refusé est rapporté sous la forme relative au vault
envoyée par le client ; le chemin résolu sur l'hôte n'est journalisé que localement.

Des adaptateurs de lecture et d'écriture à périmètre médient les opérations filesystem, tandis
que les résultats d'index, la résolution de chunk, les backlinks, les ressources, les
métadonnées d'audit et la racine de recherche ripgrep fixe sont vérifiés contre la même
dépendance de périmètre. Les enregistrements du journal que renvoient `audit_query` et
`get_note_history` passent l'admission des notes, que la note existe encore ou non, et chaque
chaîne de leurs paramètres est masquée comme leur chemin.

Le lecteur sous-jacent et l'écrivain durable conservent leurs propres contrôles de confinement
de chemin. `VaultScope` est la couture de remplacement pour une future politique d'ACL ou
d'espace de noms ; l'implémentation actuelle n'est pas un mécanisme d'isolation multi-tenant.

## Expurgation des secrets

Les secrets ne devraient pas être stockés dans un vault Markdown. Utilise un gestionnaire de
secrets et active le chiffrement de volume pour le vault et le sidecar `.datacron` au repos.

`DATACRON_REDACT_SECRETS` accepte :

- `off` : aucune expurgation optionnelle FileLogger ni de récupération ;
- `log` : expurgation FileLogger seulement ;
- `retrieval` : expurgation à la récupération MCP seulement ;
- `all` : les deux frontières, et la valeur par défaut conservatrice.

Le journal d'opérations durable expurge toujours les valeurs détectées, indépendamment de
cette politique optionnelle. Cela empêche qu'un réglage d'audit rende des identifiants clairs
durables. L'historique exact des notes n'est pas expurgé car il constitue le matériau source
réversible, pas un journal de sortie.

Le détecteur par défaut couvre les mots de passe étiquetés, les tokens, les clés et
empreintes (y compris les clés composées ou entre guillemets comme `DB_PASSWORD=`,
`"api_key":` ou `secret_key:`, et les libellés `mot de passe` et `mdp`), les identifiants
Bearer et Basic, les mots de passe placés dans une URL, les préfixes de token courants (GitHub,
GitLab, Slack, Stripe, clés d'API Google, clés de type OpenAI, JWT), les clés d'accès AWS, les
clés privées PEM et PGP et les slugs de titre porteurs de secrets. Des expressions régulières
supplémentaires peuvent être fournies sous forme de liste JSON de chaînes dans
`DATACRON_SECRET_REDACTION_PATTERNS`. Une expression personnalisée peut définir un groupe nommé
`secret` pour préserver le contexte de la correspondance ; sinon la correspondance complète est
remplacée.

Exemple :

```powershell
$env:DATACRON_REDACT_SECRETS = "all"
$env:DATACRON_SECRET_REDACTION_PATTERNS = '["INTERNAL-[0-9]{8}"]'
```

L'expurgation est une prévention de perte déterministe, pas une validation d'identifiants ni un
nettoyage du vault. Des faux positifs sont possibles avec la valeur par défaut conservatrice.

Les métadonnées des titres sont vérifiées dans la note parente complète, y compris
avec les motifs multilignes personnalisés. Lorsqu'un titre protégé contribue au
chemin d'un chunk, les cartes, recherches, backlinks et pointeurs de navigation
renvoient un alias opaque à la place du slug du titre. Transmets cet alias inchangé
à `get_note` : il retrouve le chunk indexé et conserve les contrôles de fraîcheur.
Les identifiants stockés, les octets des notes et leurs hashes restent inchangés.
Les snippets construits par la recherche ne peuvent pas réintroduire le titre
protégé dans leurs libellés de contexte.

## Sortie du hash de contenu

`get_note` renvoie aussi le hash de note exact aux octets sous `content_hash` et
`note_content_hash`, plus l'identifiant `content_hash_contract`. Les lectures par chunk
renvoient `chunk_content_hash`, le SHA-256 du contenu de chunk dérivé indexé. Ces champs sont
des digests hexadécimaux minuscules de longueur fixe, pas le contenu brut d'une note ou d'un
chunk. Ils ne contournent ni l'expurgation à la récupération, ni le sandboxing, ni les
contrôles de périmètre du vault ; seuls les champs porteurs de contenu restent soumis à ces
frontières.

## Divulgation des erreurs

Une erreur système inattendue rend la même enveloppe opaque, quel que soit l'outil :

```json
{"error": {"type": "RuntimeError", "message": "internal error",
           "code": "internal_error", "correlation_id": "d5eb466345ad"}}
```

Rien du système de fichiers hôte ne franchit la surface - ni `errno`, ni `winerror`, ni chemin,
ni `strerror`. C'est la frontière, et elle est délibérée : l'appelant d'une surface MCP n'a pas à
recevoir une carte de la machine qui le sert.

Le détail n'est pas perdu, il est local. L'exception complète, traceback compris, part dans le
FileLogger sous le même `correlation_id` que porte la charge utile, de sorte qu'un opérateur qui
tient une erreur peut retrouver la ligne de journal qui l'explique. Citer le `correlation_id`,
pas le message : `code` est le champ stable sur lequel brancher, le message est de la prose.

Le lire avec `error.get("code")`. La plupart des charges d'erreur ne portent aucun `code` - un
argument refusé ou une note absente ne rendent que `type` et `message` - de sorte que le champ
marque les classes d'échec sur lesquelles il vaut la peine de brancher, pas toutes les erreurs.

## Capacités d'outils auditées

Le manifeste fermé est `datacron.mcp.security_manifest.MCP_TOOL_CAPABILITIES`. La propriété
bloquante sur la surface d'injection le compare au registre `MCPServer` vivant. La seule capacité
adossée à un processus est `search_regex`, qui démarre l'exécutable ripgrep configuré avec des
arguments de motif et de glob fournis explicitement par l'appelant. Quand cet exécutable ne peut
pas être lancé, `search_regex` ne démarre aucun processus : il compile le motif fourni par
l'appelant avec le module `re` de Python et l'évalue contre les corps de chunks indexés dans ce
processus. Ce chemin refuse les motifs trop longs et les formes catastrophiques connues avant
toute lecture de l'index, et borne le balayage par une échéance observée entre les lots confiés
au thread de travail, mais c'est une garde best-effort et non un bac à sable : c'est la raison
pour laquelle ripgrep reste le chemin supporté. Aucun outil MCP ne fournit d'accès réseau,
d'exécution de processus arbitraire, d'`eval` ni de dispatch dynamique d'outil.

La frontière serveur n'accède à aucun gestionnaire privé du SDK. Elle délègue à l'API publique
`MCPServer.call_tool` puis traduit un nom d'outil inconnu en erreur JSON-RPC `-32602`. Une
ressource absente utilise également `-32602`, tandis qu'une panne interne de lecture de ressource
est assainie en `-32603` sans exposer le détail de l'exception.
