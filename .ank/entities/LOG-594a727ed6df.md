---
id: LOG-594a727ed6df
type: log
title: "verrou file_lock(preset) autour des cycles lecture-modif-ecriture : _layout_write/_letterbox_write"
created: 2026-10-11T11:35:24Z
author: w-61002cfd243f
scope:
  - clipper/web/app.py
  - clipper/worker.py
  - clipper/channel.py
  - tests/test_web*.py
  - tests/test_worker.py
  - tests/test_channel.py
about: TASK-61002cfd243f
seq: 2
schema: 4
version: 1
---

 (web), _sync_channel_mode (worker), migrate_legacy_presets ; save_channel(locked=True) evite l'auto-blocage ; tests rouges puis verts
