# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""English and French labels for persistent, plugin-free library pages."""

TEXT = {
    "en": {
        "home": "Knowledge library",
        "folder_title": "Navigation: {subject}",
        "subjects": "Subjects and folders",
        "active": "Current references (validity to verify)",
        "historical": "History and replaced references",
        "review": "References to verify",
        "tasks": "Open checkboxes in the sources",
        "people": "People",
        "procedures": "Procedures and references",
        "sources": "Sources",
        "empty": "No matching item in this snapshot.",
        "snapshot": (
            "Snapshot: {date}. Scope: {scope}. Follow source links for authoritative details."
        ),
        "notice": (
            "This page is maintained by Datacron. Edit the source notes; refresh this "
            "index afterwards."
        ),
        "offline": (
            "Open this folder as an Obsidian vault. Navigation uses ordinary Markdown "
            "links. External websites require a connection; missing local attachments "
            "are listed in the review report."
        ),
        "template": "Subject note template",
        "template_body": (
            "# Subject\n\nBrief description.\n\n## Current situation\n\nVerified state, "
            "verification date and source links.\n\n## Open actions\n\nAction, owner, known "
            "due date and source. Keep one authoritative action.\n\n## Decisions in "
            "force\n\nDecision, reason and source.\n\n## Useful documents\n\nProcedures and "
            "references.\n\n## History\n\nLinks to replaced decisions and earlier states.\n"
        ),
        "report": "Library review",
        "evidence": (
            "Mechanical checks only. Editorial proposals require source-by-source "
            "review; hashes do not establish truth. Original source paths and bodies are "
            "retained. No vault files have been changed by preparation."
        ),
        "next": (
            "Review preview and changes.diff; run library check against the live vault. "
            "Apply only through apply_organization_manifest (validate, then apply the "
            "same bundle/token), with other writers stopped and a verified backup. "
            "Verify receipts, reread notes and verify incoming links afterwards."
        ),
    },
    "fr": {
        "home": "Bibliothèque de connaissances",
        "folder_title": "Navigation : {subject}",
        "subjects": "Sujets et dossiers",
        "active": "Références courantes (validité à vérifier)",
        "historical": "Historique et références remplacées",
        "review": "Références à vérifier",
        "tasks": "Cases ouvertes dans les sources",
        "people": "Personnes",
        "procedures": "Procédures et références",
        "sources": "Sources",
        "empty": "Aucun élément correspondant dans cet instantané.",
        "snapshot": (
            "Instantané : {date}. Périmètre : {scope}. Suivre les liens vers les sources "
            "pour le détail faisant foi."
        ),
        "notice": (
            "Cette page est entretenue par Datacron. Modifier les notes sources, puis "
            "actualiser cet index."
        ),
        "offline": (
            "Ouvrir ce dossier comme vault Obsidian. La navigation utilise des liens "
            "Markdown ordinaires. Les sites externes nécessitent une connexion ; les "
            "pièces jointes locales manquantes figurent dans le rapport de revue."
        ),
        "template": "Modèle de fiche sujet",
        "template_body": (
            "# Sujet\n\nDescription courte.\n\n## Situation actuelle\n\nÉtat vérifié, date de "
            "vérification et liens vers les sources.\n\n## Actions ouvertes\n\nAction, "
            "responsable, échéance connue et source. Conserver une seule action faisant "
            "foi.\n\n## Décisions en vigueur\n\nDécision, raison et source.\n\n## Documents "
            "utiles\n\nProcédures et références.\n\n## Historique\n\nLiens vers les décisions "
            "remplacées et les anciens états.\n"
        ),
        "report": "Revue de la bibliothèque",
        "evidence": (
            "Contrôles mécaniques uniquement. Les propositions éditoriales demandent une "
            "revue source par source ; les empreintes ne prouvent pas la vérité. Les "
            "chemins et corps des notes sources sont conservés. La préparation n'a "
            "modifié aucun fichier du vault."
        ),
        "next": (
            "Relire preview et changes.diff ; exécuter library check sur le vault "
            "courant. Appliquer uniquement par apply_organization_manifest (validate, "
            "puis apply du même bundle/token), autres writers arrêtés et sauvegarde "
            "vérifiée. Vérifier les reçus, relire les notes et contrôler les liens "
            "entrants après application."
        ),
    },
}
