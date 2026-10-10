---
id: LOG-992aff12ac5a
type: log
title: "Mesuré: run_if_due relit déjà load_config() sans config injectée (test vert d'emblée, ajouté). Le"
created: 2026-10-10T21:14:25Z
author: w-abc31eb1723e
scope:
  - clipper/repartition.py
  - clipper/web/app.py
  - tests/test_repartition.py
  - tests/test_web.py
about: TASK-abc31eb1723e
seq: 2
schema: 4
version: 1
---

 gel vient de worker.py:_repartition_due qui injecte self.config (lu une fois à __init__) : worker.py hors scope de cette tâche -> à traiter par une tâche dédiée (relire le fichier au mtime). Web: validate_repartition rejoue line_error par ligne (409, rien créé), _rep_view fusionne refus d'exclusion et marque created sans prévisualiser les lignes créées (M2). 4 tests rouges avant correctif, verts après.
