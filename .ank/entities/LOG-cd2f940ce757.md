---
id: LOG-cd2f940ce757
type: log
title: "P2 CONFIRMÉ: enqueue(resume=True, action run) relançait _start_thumbnail_fetch à chaque retry_at"
created: 2026-10-11T11:35:16Z
author: w-38a299091bd4
scope:
  - clipper/worker.py
  - clipper/pipeline.py
  - clipper/web/app.py
  - clipper/learning.py
  - clipper/__main__.py
  - tests/test_worker.py
  - tests/test_pipeline*.py
  - tests/test_web*.py
  - tests/test_learning.py
about: TASK-38a299091bd4
seq: 3
schema: 4
version: 1
---

 (test rouge). Correctif: pas de fil yt-dlp pour une reprise.
