---
id: TASK-9e2023ecbc23
type: task
slug: re-rendu-garder-les-champs-de-publication-du-sid
title: "Re-rendu : garder les champs de publication du sidecar (tiktok_post...)"
created: 2026-10-12T13:43:59Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/render.py
  - tests/test_render.py
blocked_by: []
done_criteria: |
  Un re-rendu forcé conserve tiktok_post, youtube_post, removed_from_platform et les autres champs de publication/apprentissage recensés de l'ancien sidecar ; tests unitaires verts
criteria_by: creator
verify: [tests]
method: tdd
proof:
  - type: test
    ref: local/2d56135d2ae8@46f703a
    tree: scope/4d278c6b1d3e
    criteria: fccef223a595
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---

## Bug
`clipper/render.py` (fin de `render_clip`, ~l.1520) réécrit `output/<video>/<clip>.json` de zéro. Un re-rendu (`--force-step reframe`, `/api/videos/<id>/retry` from_step) efface donc les champs écrits APRÈS le rendu par d'autres modules : `tiktok_post` (publish.py l.543/655, learning.py l.241), `youtube_post`, `removed_from_platform`, et tout autre champ de publication/apprentissage. Conséquences : un clip publié ou programmé paraît neuf (repartition le replanifie → doublon TikTok), learning perd le lien post→clip.

## À faire
1. Recenser (grep) tous les champs que publish.py, learning.py, tiktok.py, youtube*.py, repartition.py, web/ écrivent dans un sidecar de clip ; les lister dans une constante de render.py (ex. `_KEPT_SIDECAR_FIELDS`) avec un commentaire qui cite qui les écrit.
2. Au rendu, si l'ancien sidecar existe et se lit : recopier ces champs tels quels dans le nouveau. Sidecar ancien illisible (JSON cassé) : erreur explicite RenderError (ADR-ad2e, pas d'écrasement silencieux de l'état de publication).
3. Tests (tests/test_render.py, sans ffmpeg réel si les tests existants ont déjà un moyen ; sinon tester une fonction pure extraite) : champ conservé après re-rendu forcé ; champs de rendu bien renouvelés (screen_title, layout, qa remis au défaut, created_at) ; absence d'ancien sidecar = comportement actuel ; ancien sidecar cassé = erreur.
Ne lancer aucun rendu réel.
