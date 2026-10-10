---
id: LOG-2a574d2ad28b
type: log
title: "Observe (rouge avant correctif) : test_reload_video_removes_only_on_404_and_toasts_other_errors[500]"
created: 2026-10-10T23:46:17Z
author: w-94f4bb2a2e94
scope:
  - clipper/web/static/app.js
  - tests/test_web.py
about: TASK-94f4bb2a2e94
seq: 2
schema: 4
version: 1
---

 echoue : sortie {videos:['v2'], toasts:[]} ; cause : reloadVideo (app.js) attrape toute erreur d'api() et retire la video (catch vide). 404 passe (baseline). api() ne transmet pas le statut HTTP a l'erreur.
