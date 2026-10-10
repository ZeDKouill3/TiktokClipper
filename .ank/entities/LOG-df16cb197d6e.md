---
id: LOG-df16cb197d6e
type: log
title: "Reserve documentee : une entree running / un battement / une entree en cours ecrits AVANT cette"
created: 2026-10-10T18:55:01Z
author: w-4ca998e97789
scope:
  - clipper/__main__.py
  - clipper/publish.py
  - clipper/worker.py
  - tests/test_publish.py
  - tests/test_worker.py
about: TASK-4ca998e97789
seq: 4
schema: 4
version: 1
---

 version n'ont pas d'heure de creation : process_alive retombe sur l'existence du pid (comportement d'avant), sinon test_web.py (hors scope : entrees sans pid_created_at, cancel tue l'enfant, battement actif) casserait et une mise a jour avec un enfant encore vivant relancerait la video a cote (coeur-I1). Fenetre unique a la mise a jour ; toute entree ecrite ensuite porte l'heure. Le script repro_pid_reuse_cancel de l'audit (entree sans pid_created_at) reste donc 'rouge' par construction ; le test test_cancel_does_not_kill_a_process_whose_creation_time_differs couvre le cas reel. Aucun defaut juge faux ; aucune decision humaine requise pour A1.
