---
id: TASK-0b013cbf1bdd
type: task
slug: ui-v3-u2-coquille-v3-v3-css-jetons-clair-sombre
title: "UI v3 U2 : Coquille v3 : v3.css (jetons clair/sombre, layout, nav + rail, topbar, tabbar, pastilles), index.html (sections intactes, classe v3 sur la coquille), thème clair par défaut, toasts Basecoat"
created: 2026-10-11T03:59:30Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/web/static/v3.css
  - clipper/web/static/index.html
  - clipper/web/static/app.js
  - clipper/web/static/ui.js
  - clipper/web/static/screens.js
  - clipper/web/static/style.css
  - tests/test_web*.py
blocked_by: [TASK-5b54d0a590bb]
done_criteria: |
  index.html garde nav.nav, nav.tabbar, id="screen-<x>", ordre des scripts ; thème par défaut light quand localStorage est vide (test node sur la fonction de choix) ; toast() avec undo produit un bouton « Annuler » (node) ; renderCurrent n'assigne plus innerHTML (grep négatif) ; palette : fonction paletteItems(query, store) testée sous node (écrans, vidéos par titre/id, actions) ; 44px et @media (max-width: 900px) présents dans v3.css ; aucune heure sans timeZone Contexte : ADR-a308c4cafb5b et SPEC-7715813cad25 (lire par ank show). Plan détaillé (ligne U2 du tableau, parties 3 et 5) : E:\ClaudeRandom\TiktokParseUpload\research\drafts\ui-v3\PLAN.md ; direction visuelle DIRECTION.md, maquettes cliquables maquettes/ (index.html, captures/) dans le même dossier : les lire, ne rien copier de research/ dans le dépôt (noms réels). Règles : une tâche ne supprime jamais un test sans le remplacer par un équivalent nommé ; aucune ressource externe ; heure de Paris ; aucun nom réel (fixtures ma_chaine, compte_alpha) ; le bloc CSS de l'écran est retiré de style.css entre ses délimiteurs /* >>> x */ … /* <<< x */.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---
