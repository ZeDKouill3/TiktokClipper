---
id: LOG-c86d197d65db
type: log
title: "MESURE 19/10 : state/browser/<id>/Default/Preferences profile.exit_type = Crashed pour 1ad53325b65d"
created: 2026-10-19T08:19:33Z
author: w-08c8c051855c
scope:
  - clipper/tiktok.py
  - clipper/browser.py
  - clipper/worker.py
  - tests/**
about: TASK-08c8c051855c
seq: 2
schema: 4
version: 1
---

 (ClipperFou), Normal pour 17c3783183e1, 1ab74eb871e0, 5fb241d5fb53. Cause probable : serve/worker tué avec Chrome ouvert (relevé de stats ClipperFou toutes ~3 min : state/stats/tiktok/1ad53325b65d/*.json du 18-19/10) -> profil Crashed -> au relancement Chrome restaure/referme des onglets ; tiktok.py:1491 et 1507 prennent context.pages[0] (onglet restauré) qui est fermé par Chrome au démarrage -> 'Target page, context or browser has been closed'. Journaux 14-17/10 absents (seuls 17-19 existent, sans cette erreur) : cause établie par mesure du profil + lecture du code, pas par la trace brute. Correctif : ne jamais réutiliser un onglet préexistant, ouvrir une page neuve puis fermer les anciennes.
