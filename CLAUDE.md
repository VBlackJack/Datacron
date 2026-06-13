# CLAUDE.md — Auto-loaded context for Claude Code sessions in this repo

> Ce fichier est lu automatiquement par Claude Code au démarrage de toute session
> dans `G:\_Projects\Datacron\`. Il aiguille l'agent vers les bons docs et active
> les bons réflexes.

## Si le MCP `datacron` est actif

Avant toute autre action, charge silencieusement le contexte mémoire :

```
list_notes(folder="_memory", limit=30)
get_note("_memory/preferences/julien.md")
get_note("_memory/projects/datacron.md")
```

Ces memories vivent dans `G:\_DATA\_memory\` (vault de Julien, indexé par Datacron).
Elles sont la source de vérité pour l'état courant du projet, pas le code seul.

## Si le MCP `datacron` n'est pas actif

Avertis Julien : il manque la mémoire active. Suggère-lui de lancer le serveur
(`datacron mcp serve`) ou de vérifier la config Claude Desktop.

## Documents clés du repo (lecture sélective selon la tâche)

| Tâche | Lire |
|---|---|
| Onboarding général | `README.md`, `docs/ARCHITECTURE.md` |
| Pourquoi telle décision | `docs/decisions-tranchees-v2.1.md` |
| Implémenter du code | `docs/agent-briefs/00-shared-context.md` + `01-contracts.md` |
| Brief Claude Code spécifique | `docs/agent-briefs/02-brief-claude-code.md` |
| Brief Codex spécifique | `docs/agent-briefs/03-brief-codex.md` |
| Planning de tour | `docs/agent-briefs/04-integration-plan.md` |
| Reviews croisées | `docs/reviews/sem*/...` |
| Conventions vault | `SPEC.md` |

## Workflow Datacron (rappel)

- Deux branches actives : `claude-code/phase0`, `codex/phase0`
- Merge `--no-ff` vers `main` chaque lundi après cross-review weekend
- Tags : `fast-track/week1-core`, `sem1-complete`, `sem2-complete`, `sem3-complete`
- **Règle stricte** : no cross-territory fixes (flag in review only)
- Contracts figés dans `docs/agent-briefs/01-contracts.md`

## Standards de code (rappel court)

Source : skills `dev-standards` + `bash-standards`.

- Apache 2.0 header sur tout `.py` / `.sh`
- English partout (code, comments, identifiers, commits)
- Zero hardcoding (env vars, constants)
- FileLogger Python (pas de `print`)
- `ruff` + `ruff format --check` + `mypy --strict` + `pytest` doivent passer
- Conventional commits en EN forme impérative

## Profil Julien

TDAH. Format court, valide options avant action longue, une étape à la fois.
Pas de tableau-à-3-scénarios-suivi-de-4-options qui crée paralysie. Tutoiement.
Documentation FR, code EN.

## Memory layer — auto-write

Tu écris dans `G:\_DATA\_memory\` **automatiquement** quand tu identifies une
info durable, sans demander à Julien la permission. Critères et conventions
détaillés dans `G:\_DATA\_memory\README.md` § "auto-write par défaut".

**Résumé** :
- Décisions tranchées, faits confirmés, préférences, conventions, changements
  d'état projet → écris ou update
- Brainstorm, opinions ponctuelles, discussions hypothétiques → n'écris pas
- Si folder pas monté : `request_cowork_directory("G:\\_DATA")` une fois
- Update une note existante quand le sujet est déjà couvert (pas de duplication)
- Notification discrète en fin de réponse : `📝 _memory: <action> <chemin>`
- Ne suggère pas `datacron index` à chaque écriture — c'est du bruit. Seulement
  fin de session, ou quand tu veux tester search dessus.
