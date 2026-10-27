---
id: LOG-ba36db44d1ba
type: log
title: "capture 23:21 : Minutes 30 deja aria-selected=true (la roue choisit l'element centre sans clic) ;"
created: 2026-10-27T00:04:54Z
author: w-83bd9b97938d
scope:
  - clipper/tiktok.py
  - clipper/assets/tiktok_selectors.toml
  - tests/test_tiktok.py
  - tests/fixtures
about: TASK-83bd9b97938d
seq: 2
schema: 4
version: 1
---

 copie de la capture en fixture refusee, fixtures existantes (timewheel_open/selected) utilisees ; plan : centrage, attente immobilite (bounding_box x2), relecture aria-selected, clic simple reessaye 3x apres recentrage, arret avec capture
