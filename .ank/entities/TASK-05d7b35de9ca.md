---
id: TASK-05d7b35de9ca
type: task
slug: ui-v3-u11-comptes-v3-table-tiroir-d-dition
title: "UI v3 U11 : Comptes v3 (table, tiroir d'édition)"
created: 2026-10-11T03:59:37Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/web/static/screens/accounts.js
  - clipper/web/static/screens/accounts.css
  - clipper/web/static/v3.css
  - clipper/web/static/index.html
  - tests/test_web*.py
blocked_by: [TASK-0b013cbf1bdd]
done_criteria: |
  Table avec colonnes connexion / prêt / créneaux / posts ; au plus 3 boutons visibles par ligne, le reste dans un menu (test node compte les <button hors [role=menu]) ; note de pause affichée une seule fois Contexte : ADR-a308c4cafb5b et SPEC-7715813cad25 (lire par ank show). Plan détaillé (ligne U11 du tableau, parties 3 et 5) : E:\ClaudeRandom\TiktokParseUpload\research\drafts\ui-v3\PLAN.md ; direction visuelle DIRECTION.md, maquettes cliquables maquettes/ (index.html, captures/) dans le même dossier : les lire, ne rien copier de research/ dans le dépôt (noms réels). Règles : une tâche ne supprime jamais un test sans le remplacer par un équivalent nommé ; aucune ressource externe ; heure de Paris ; aucun nom réel (fixtures ma_chaine, compte_alpha) ; le bloc CSS de l'écran est retiré de style.css entre ses délimiteurs /* >>> x */ … /* <<< x */.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---
