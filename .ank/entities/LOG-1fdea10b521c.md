---
id: LOG-1fdea10b521c
type: log
title: "Repro (python -I, CPU, FakeBackend) : I1 _stable_face_over_null([hud, cloud],"
created: 2026-10-10T21:02:35Z
author: w-cee612eefa16
scope:
  - clipper/reframe.py
  - tests/test_reframe.py
about: TASK-cee612eefa16
seq: 3
schema: 4
version: 1
---

 periods=[[hud,cloud]]) -> retenu 4 (override de null). M2 scan _size_camera_rect bh 16..400 : 18 (w,h) refuses par |w/h - 1.40625| > 0.028125, 112x78 ecart 0.02965. M5 : fichier facecam/period_7.jpg pre-existant survit a detect_facecam. 6 tests de regression ROUGES ecrits (test 2948 inverse + 5 nouveaux en fin de tests/test_reframe.py).
