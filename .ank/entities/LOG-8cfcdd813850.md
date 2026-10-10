---
id: LOG-8cfcdd813850
type: log
title: "diagnose A2: test arbre rouge (petit-enfant vivant après cancel, TerminateProcess du seul pid) ;"
created: 2026-10-10T20:27:33Z
author: w-3a12bbabc977
scope:
  - clipper/__main__.py
  - clipper/worker.py
  - tests/test_worker.py
about: TASK-3a12bbabc977
seq: 2
schema: 4
version: 1
---

 fix: _kill_descendants (taskkill /T /F | killpg, enfants lancés en nouvelle session hors Windows) appelé par _terminate_pid ; serve termine l'arbre du worker via worker.terminate_tree ; 287 tests test_worker verts
