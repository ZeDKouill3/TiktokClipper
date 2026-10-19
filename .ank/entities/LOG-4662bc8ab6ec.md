---
id: LOG-4662bc8ab6ec
type: log
title: "Cause mesurée: liste Publications virtualisée (~5 lignes) triée par date décroissante;"
created: 2026-10-19T07:48:02Z
author: w-42a56837bbc6
scope:
  - clipper/tiktok.py
  - clipper/worker.py
  - clipper/publish.py
  - tests/**
about: TASK-42a56837bbc6
seq: 2
schema: 4
version: 1
---

 _lookup_post_link ne lisait que les premières lignes, le post programmé (date future) est plus bas que les autres programmés. Relevé réel 1ad53325b65d/20261019T073714Z contient le post 7698279152587148566 avec lien. Fix: défilement scroll_script dans _lookup_post_link; 2 tests rouges avant, verts après; 870 tests ciblés OK.
