---
id: LOG-a2cb4eb50c68
type: log
title: "Cause mesuree: periode 1 candidat 6 repli visage (0,744,472,336); refine cale haut=766 droit=558 ->"
created: 2026-10-11T11:25:35Z
author: w-999bc55655f6
scope:
  - clipper/reframe.py
  - tests/test_reframe.py
about: TASK-999bc55655f6
seq: 2
schema: 4
version: 1
---

 boite 558x314, _size_camera_rect agrandit au format panneau (h 396) centre y=923 puis clamp bas => y=684 (82 px de jeu). Fix: _cover_camera_rect rogne a l'interieur de l'incrustation (refine + bords reels). Test rouge sans fix: y=684 >= 764 faux. test_reframe.py 195 verts; 5 tests 'couvre' passes en 'inside'.
