---
title: Frontière de sécurité locale de Datacron
verified: 2026-08-11
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

`SingleTenantVaultScope` autorise aujourd'hui les lectures dans tout le vault configuré. Les
écritures doivent en plus tomber dans une racine `DATACRON_WRITE_PATHS` explicite. Des
adaptateurs de lecture et d'écriture à périmètre médient les opérations filesystem, tandis que
les résultats d'index, la résolution de chunk, les backlinks, les ressources, les métadonnées
d'audit et la racine de recherche ripgrep fixe sont vérifiés contre la même dépendance de
périmètre.

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
empreintes, les identifiants Bearer, les préfixes de token courants, les clés d'accès AWS, les
clés privées PEM et les slugs de titre porteurs de secrets. Des expressions régulières
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

## Capacités d'outils auditées

Le manifeste fermé est `datacron.mcp.security_manifest.MCP_TOOL_CAPABILITIES`. La propriété
bloquante sur la surface d'injection le compare au registre `MCPServer` vivant. La seule capacité
adossée à un processus est `search_regex`, qui démarre l'exécutable ripgrep configuré avec des
arguments de motif et de glob fournis explicitement par l'appelant. Aucun outil MCP ne fournit
d'accès réseau, d'exécution de processus arbitraire, d'`eval` ni de dispatch dynamique d'outil.

La frontière serveur n'accède à aucun gestionnaire privé du SDK. Elle délègue à l'API publique
`MCPServer.call_tool` puis traduit un nom d'outil inconnu en erreur JSON-RPC `-32602`. Une
ressource absente utilise également `-32602`, tandis qu'une panne interne de lecture de ressource
est assainie en `-32603` sans exposer le détail de l'exception.
