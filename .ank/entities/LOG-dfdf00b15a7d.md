---
id: LOG-dfdf00b15a7d
type: log
title: "Reproduit publication-I1 : entree failed+to_verify (mark_failed to_verify=True, comme"
created: 2026-10-10T20:27:15Z
author: w-14699025fdd6
scope:
  - clipper/publish.py
  - clipper/web/app.py
  - clipper/web/static/screens/publish.js
  - tests/test_publish.py
  - tests/test_web.py
about: TASK-14699025fdd6
seq: 2
schema: 4
version: 1
---

 Worker._unconfirmed). move/unschedule/update_post/cancel_post/set_mode : 5 appels acceptes, 0 PublishError (6 tests rouges, 'DID NOT RAISE'). Mesure : move -> status scheduled, to_verify True conserve ; Worker.tick() -> 1 publication (mode immediate), entree 'published' : le worker ne lit que status. Cause : aucune des 5 fonctions ne lit to_verify, seul retry le remet a False.
