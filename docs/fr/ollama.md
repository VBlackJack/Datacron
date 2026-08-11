---
title: Utiliser Datacron avec Ollama
verified: 2026-08-11
tested_on: "Windows 11 / Ollama 0.32.6 / mcpo 0.0.20 / MCP 1.28.1 / Datacron 2026.0721.01"
---

# Utiliser Datacron avec Ollama

**Français** | [English](../en/ollama.md)

> Ollama fournit le modèle et son API de tool calling. Un client ou un pont séparé doit
> découvrir et exécuter les outils MCP de Datacron.

## Ce qui est vérifié

Ollama n'est pas un hôte MCP. Son API reçoit des descriptions de fonctions, renvoie des
appels de fonctions, puis attend que le client exécute ces appels et lui retourne les
résultats. Datacron expose pour sa part un serveur MCP local sur le transport `stdio` via
la commande `datacron-mcp`. Un pont doit donc relier ces deux surfaces.

Les niveaux de preuve de cette page sont distincts :

| Voie | Etat au 11 août 2026 |
|---|---|
| `ollmcp` | Vérifié sur la documentation officielle, mais non exécuté de bout en bout : sa TUI `prompt_toolkit` exige un vrai écran console Windows et ne démarre pas dans le canal headless utilisé pour le test. |
| `mcpo` | Transport `stdio` vers OpenAPI testé localement avec Datacron : découverte de 17 routes, puis appels réels de `get_health`, `search_text` et `get_note`. |
| Open WebUI avec Ollama | Compatibilité et configuration vérifiées sur la documentation officielle, mais Open WebUI local n'a pas été reconfiguré pendant le test. |

## Préparer Datacron en lecture seule

Initialisez et indexez d'abord le vault avec le
[guide d'installation](setup.md). Dans le processus qui lance le pont, définissez seulement
la racine et l'allowlist de lecture :

```powershell
$env:DATACRON_VAULT_ROOT = "<YOUR_VAULT>"
$env:DATACRON_READ_PATHS = "<YOUR_VAULT>"
Remove-Item Env:DATACRON_WRITE_PATHS -ErrorAction SilentlyContinue
```

Ne définissez pas `DATACRON_WRITE_PATHS` pour cette première connexion. Les outils
d'écriture restent enregistrés, mais Datacron les refuse parce qu'aucune racine d'écriture
n'est configurée.

## Option 1 - client direct `ollmcp`

`ollmcp` est un client MCP interactif conçu pour Ollama. Sa documentation annonce les
transports `stdio`, SSE et Streamable HTTP, un format `mcpServers` compatible avec la
commande, les arguments et l'environnement, ainsi qu'une confirmation humaine des appels
d'outils activée par défaut.

Installation documentée par le projet :

```powershell
uv tool install ollmcp==0.33.2
```

Exemple de configuration séparée, sans écrire dans la configuration globale d'`ollmcp` :

```json
{
  "mcpServers": {
    "datacron": {
      "command": "<PATH_TO_DATACRON_MCP>",
      "args": [],
      "env": {
        "DATACRON_VAULT_ROOT": "<YOUR_VAULT>",
        "DATACRON_READ_PATHS": "<YOUR_VAULT>",
        "DATACRON_DURABILITY": "best-effort"
      }
    }
  }
}
```

Lancement documenté :

```powershell
ollmcp --servers-json <PATH_TO_SERVERS_JSON> `
  --provider ollama `
  --host http://localhost:11434 `
  --model <TOOL_CAPABLE_MODEL>
```

Cette commande est vérifiée sur la documentation amont, mais non exécutée dans le smoke
headless de cette page. Une validation interactive doit encore confirmer la découverte des
outils et les appels avec le modèle choisi avant de qualifier cette voie de testée localement.

## Option 2 - Open WebUI avec le pont `mcpo`

Open WebUI prend en charge Ollama. Son support MCP natif accepte Streamable HTTP, pas un
serveur local `stdio` comme Datacron. Le projet Open WebUI fournit `mcpo` pour convertir un
serveur MCP `stdio` en API OpenAPI consommable par l'interface.

Le lancement testé localement est le suivant, avec des placeholders pour les valeurs qui
dépendent de la machine :

```powershell
uvx --with mcp==1.28.1 mcpo==0.0.20 `
  --host 127.0.0.1 `
  --port <LOCAL_PORT> `
  --api-key <TEMP_API_KEY> `
  --strict-auth `
  -- <PATH_TO_DATACRON_MCP>
```

Le pin `mcp==1.28.1` est nécessaire dans l'état mesuré. Le lancement non contraint
`uvx mcpo==0.0.20` a résolu MCP 2.0.0 et a échoué avant le démarrage : `mcpo` importe encore
`streamablehttp_client`, symbole retiré de MCP 2.0.0. Ne retirez pas ce pin sans refaire le
smoke.

Résultat réel du smoke sur un vault temporaire contenant une note :

```text
OpenAPI routes: 17
get_health: status=healthy, notes_count=1, consistent_with_vault=true
search_text: returned=1, marker=MCPO_DATACRON_SMOKE_20260811
get_note: rel_path=sentinel.md, marker=MCPO_DATACRON_SMOKE_20260811
write_paths_configured=false, effective_writes_enabled=false
```

Le proxy était lié à `127.0.0.1`, protégé par une clé temporaire, et arrêté après le test.
Ajoutez ensuite son URL comme serveur d'outils OpenAPI dans Open WebUI. Cette dernière étape
et la restitution du résultat par un modèle Ollama restent non mesurées localement dans ce lot.

## Option écartée - `mcphost`

`mcphost` n'est pas recommandé pour une nouvelle intégration. Son dépôt officiel est archivé
depuis le 13 avril 2026, en lecture seule, et indique qu'il ne recevra plus de mises à jour ni
de correctifs. Les deux voies ci-dessus conservent donc seules une place dans ce guide.

## Limite des petits modèles

La présence du tool calling dans Ollama ne garantit pas qu'un modèle choisira le bon outil,
produira des arguments valides ou terminera correctement une suite de plusieurs appels. Cette
qualité dépend du modèle et du prompt ; son évaluation reste séparée dans BL-0019. Cette page
ne certifie donc aucun modèle particulier.

## Références

- [API officielle Ollama](https://docs.ollama.com/api/introduction) - endpoint modèle local.
- [Tool calling Ollama](https://docs.ollama.com/capabilities/tool-calling) - boucle d'exécution côté client.
- [Dépôt officiel ollmcp](https://github.com/jonigl/mcp-client-for-ollama) - installation, configuration et transports MCP.
- [MCP dans Open WebUI](https://docs.openwebui.com/features/extensibility/mcp/) - support natif Streamable HTTP et recours à `mcpo` pour `stdio`.
- [Dépôt officiel mcpo](https://github.com/open-webui/mcpo) - pont MCP vers OpenAPI et options de lancement.
- [Dépôt officiel mcphost](https://github.com/mark3labs/mcphost) - état archivé et arrêt de la maintenance.
- Test local : Windows 11, 11 août 2026, versions indiquées dans le frontmatter.
