---
id: TASK-fecba79ca3d4
type: task
slug: ui-v3-u9-styles-v3-formulaire-libell-s-menus
title: "UI v3 U9 : Styles v3 (formulaire, libellés, menus)"
created: 2026-10-11T03:59:36Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/web/static/screens/channels.js
  - clipper/web/static/screens/layout.js
  - clipper/web/static/screens/layout-letterbox.js
  - clipper/web/static/screens/chan-subtitles-preview.js
  - clipper/web/static/v3.css
  - clipper/web/static/style.css
  - tests/test_web*.py
blocked_by: [TASK-0b013cbf1bdd]
done_criteria: |
  Chaque clé technique a un libellé français (table FIELD_LABELS couvrant toutes les clés de CONFIG_DEFAULTS des sections affichées : test qui compare) ; « Supprimer » dans le menu « … » ; éditeur d'agencement inchangé (tests existants) Contexte : ADR-a308c4cafb5b et SPEC-7715813cad25 (lire par ank show). Plan détaillé (ligne U9 du tableau, parties 3 et 5) : E:\ClaudeRandom\TiktokParseUpload\research\drafts\ui-v3\PLAN.md ; direction visuelle DIRECTION.md, maquettes cliquables maquettes/ (index.html, captures/) dans le même dossier : les lire, ne rien copier de research/ dans le dépôt (noms réels). Règles : une tâche ne supprime jamais un test sans le remplacer par un équivalent nommé ; aucune ressource externe ; heure de Paris ; aucun nom réel (fixtures ma_chaine, compte_alpha) ; le bloc CSS de l'écran est retiré de style.css entre ses délimiteurs /* >>> x */ … /* <<< x */.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---
