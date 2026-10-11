---
id: TASK-3c87a337ffc2
type: task
slug: ui-v3-u8-veille-v3
title: "UI v3 U8 : Veille v3"
created: 2026-10-11T03:59:35Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/web/static/screens/veille.js
  - clipper/web/static/screens/veille.css
  - clipper/web/static/v3.css
  - clipper/web/static/index.html
  - tests/test_web*.py
blocked_by: [TASK-0b013cbf1bdd]
done_criteria: |
  Tests veille existants passent ; sommaire avec ancres des 5 sections ; sources en table (une ligne par source avec état et erreur) ; hauteur : aucune section ne rend plus de 12 jaquettes sans « voir plus » (test node sur gamesHtml) Contexte : ADR-a308c4cafb5b et SPEC-7715813cad25 (lire par ank show). Plan détaillé (ligne U8 du tableau, parties 3 et 5) : E:\ClaudeRandom\TiktokParseUpload\research\drafts\ui-v3\PLAN.md ; direction visuelle DIRECTION.md, maquettes cliquables maquettes/ (index.html, captures/) dans le même dossier : les lire, ne rien copier de research/ dans le dépôt (noms réels). Règles : une tâche ne supprime jamais un test sans le remplacer par un équivalent nommé ; aucune ressource externe ; heure de Paris ; aucun nom réel (fixtures ma_chaine, compte_alpha) ; le bloc CSS de l'écran est retiré de style.css entre ses délimiteurs /* >>> x */ … /* <<< x */.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---
