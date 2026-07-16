# Scrubber d'intégrité

**Français** | [English](../en/integrity-scrubber.md)

Le scrubber de Datacron compare les octets du filesystem primaire avec le SHA-256 exact aux
octets stocké dans la génération d'index terminée. Il détecte les notes manquantes, les
changements de contenu, la troncature et les incohérences porteuses de NUL. Il se contente
d'alerter : il ne réécrit jamais une note, ne répare pas une ligne d'index, ne restaure pas
d'historique et ne remplace pas de sentinelle.

## Modèle d'exécution

Le scrubber est une opération CLI incrémentale explicite, pas un thread MCP en arrière-plan.
Planifie des invocations répétées avec le Planificateur de tâches Windows, cron ou un opérateur
équivalent. Chaque invocation reprend la passe courante et s'arrête à la limite de durée
configurée.

```text
datacron scrub-init --vault G:\_DATA
datacron scrub --vault G:\_DATA
```

`scrub-init` est une action de provisionnement séparée et explicite. Elle crée atomiquement les
sentinelles configurées manquantes et refuse d'écraser une sentinelle existante avec des octets
différents. `scrub` n'initialise jamais de sentinelle. Une sentinelle manquante est une alerte
critique.

`scrub` sort en code 0 lorsque la fenêtre courante n'a aucune anomalie, en code 2 quand une
anomalie est présente, et en code 1 pour un échec opérationnel. Une passe partielle en code 0
doit être relancée jusqu'à ce que la santé rapporte une couverture complète.

Aucun outil MCP n'est ajouté. `get_health` lit le point de contrôle sans démarrer de travail,
si bien que les serveurs certifiés lecture seule restent en lecture seule sur le sidecar.

## Configuration

Toutes les limites opérationnelles et tous les chemins sont des réglages runtime :

| Variable d'environnement | Défaut | Signification |
|---|---:|---|
| `DATACRON_SCRUB_NOTES_PER_SECOND` | `50` | Nombre moyen maximum de fichiers ouverts par seconde, sentinelles incluses |
| `DATACRON_SCRUB_MEBIBYTES_PER_SECOND` | `16` | Nombre moyen maximum d'octets primaires lus par seconde |
| `DATACRON_SCRUB_MAX_DURATION_SECONDS` | `30` | Durée coopérative par invocation |
| `DATACRON_SCRUB_CHECKPOINT_INTERVAL_NOTES` | `25` | Nombre de notes entre deux points de contrôle durables du curseur |
| `DATACRON_SCRUB_CHECKPOINT_PATH` | `.datacron/scrubber/checkpoint.json` | Chemin du point de contrôle, relatif au vault |
| `DATACRON_SCRUB_CANARY_DIR` | `.datacron/scrubber/canaries` | Répertoire des sentinelles, relatif au vault |
| `DATACRON_SCRUB_CANARIES` | mapping JSON | Noms de sentinelles relatifs vers contenu UTF-8 exact |

Les chemins doivent être relatifs au vault et ne peuvent pas contenir de traversée. Les noms de
sentinelles sont des chemins relatifs sûrs sous le répertoire de sentinelles configuré.

Les sentinelles par défaut sont :

```json
{
  "exact-byte-lf.md": "# Datacron integrity canary\n\nformat: utf-8-lf\nsequence: 0123456789abcdef\n",
  "exact-byte-crlf.md": "# Datacron integrity canary\r\n\r\nformat: utf-8-crlf\r\nsequence: fedcba9876543210\r\n"
}
```

Le contenu attendu vient de la configuration, tandis que les octets observés des sentinelles
viennent du répertoire sidecar du vault configuré. Par exemple, un mapping personnalisé peut
être fourni en JSON avec des octets de fin de ligne échappés.

## Point de contrôle et reprise

Le point de contrôle JSON ASCII enregistre :

- l'ID de passe et la version de schéma ;
- la génération d'index numérique et le digest déterministe de génération chemin/ID/hash ;
- le curseur de chemin trié, les notes vérifiées, les octets vérifiés et le total de notes ;
- les horodatages de début, de mise à jour et de dernière complétion ;
- la couverture des sentinelles et les enregistrements d'anomalies dédupliqués.

La reprise exige la même génération numérique, le même digest de génération et le même nombre
de notes. Un changement d'index démarre une nouvelle passe. Les points de contrôle utilisent un
remplacement atomique durable. Après un crash impossible à rattraper, jusqu'à un lot de point de
contrôle configuré peut être relu ; les enregistrements d'alerte restent idempotents et ne
peuvent pas être dupliqués.

## Contrat de santé

`get_health.scrubber` contient :

- `status` : `not_run`, `running`, `stale`, `complete` ou `critical` ;
- `last_scrub`, `pass_id` et `index_generation` ;
- la couverture en notes vérifiées, total de notes, fraction et drapeau de complétion ;
- les octets vérifiés ;
- le nombre d'anomalies et les preuves chemin/type ;
- les preuves de sentinelles vérifiées/total/saines.

Toute anomalie de scrub rend la santé de haut niveau `critical`. Sans anomalie de scrub, la
dette d'ID, de liens cassés et d'EOL mixtes reste l'état cosmétique distinct `degraded`. Un EOL
mixte n'est pas une anomalie de scrub quand ses octets courants exacts correspondent à l'index.

## Frontière d'échec et de confiance

Le contenu observé des notes et des sentinelles est lu directement depuis le filesystem réel
après autorisation `VaultScope`. Les résultats de récupération et le contenu de chunk indexé ne
sont jamais utilisés comme canal d'octets observés.

L'autorité du hash de note reste l'index dérivé. Un changement coordonné à la fois d'une note et
de son hash d'index sort de cette garantie de checksum non authentifiée. Les sentinelles
détectent une régression partagée contenu-chemin pour leurs fichiers connus, mais ne
transforment pas l'index entier en registre cryptographiquement authentifié.

Chaque anomalie est écrite dans le FileLogger et le point de contrôle. Une vérification humaine
est requise avant toute opération corrective. Cela préserve la règle FM-O-01 : ne jamais
réparer à partir d'un signal d'intégrité indirect.

## Audit des décisions de notes exclues

Génère le rapport lecture seule d'écarts d'index avec des seuils heuristiques configurables :

```text
python scripts/audit_excluded_notes.py \
  --vault-root G:\_DATA \
  --output local/excluded_notes_audit.md
```

Le rapport ne déplace, ne modifie ni n'indexe aucune note.
