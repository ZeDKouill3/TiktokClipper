---
id: TASK-e3dee736b676
type: task
slug: ui-v3-u6-publication-v3-calendrier-d-abord-rail
title: "UI v3 U6 : Publication v3 : calendrier d'abord, rail À faire, dialogues, agenda mobile"
created: 2026-10-11T03:59:33Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/web/static/screens/publish.js
  - clipper/web/static/v3.css
  - clipper/web/static/style.css
  - tests/test_web*.py
blocked_by: [TASK-0b013cbf1bdd, TASK-1679e58d021b]
done_criteria: |
  Tous les tests _run_publish existants passent (fonctions pubLocalInput, pubParisInstant, pubWhen, pubFreeSlot… conservées) ; nouveaux : calPostHtml(p) classe published/scheduled/failed, agendaHtml(days) présent sous 900 px (CSS), « Valider le plan » grisé porte title avec la raison, bandeau d'aide absent du rendu, dialogue natif pour Nouvelle publication et Série Contexte : ADR-a308c4cafb5b et SPEC-7715813cad25 (lire par ank show). Plan détaillé (ligne U6 du tableau, parties 3 et 5) : E:\ClaudeRandom\TiktokParseUpload\research\drafts\ui-v3\PLAN.md ; direction visuelle DIRECTION.md, maquettes cliquables maquettes/ (index.html, captures/) dans le même dossier : les lire, ne rien copier de research/ dans le dépôt (noms réels). Règles : une tâche ne supprime jamais un test sans le remplacer par un équivalent nommé ; aucune ressource externe ; heure de Paris ; aucun nom réel (fixtures ma_chaine, compte_alpha) ; le bloc CSS de l'écran est retiré de style.css entre ses délimiteurs /* >>> x */ … /* <<< x */.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---
