---
id: TASK-721f20f1eaa0
type: task
slug: moments-amorce-de-gameplay-avant-la-r-action-lea
title: "Moments : amorce de gameplay avant la réaction (lead_in_seconds), pour voir l'action qui la provoque"
created: 2026-10-11T10:45:53Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/moments.py
  - tests/test_moments.py
blocked_by: []
done_criteria: |
  Cas réel 11/10 (VOD gaming, style combat) : le clip démarre sur la première phrase « Camembert ! ... j'ai flash sur place ! » (11794,6 s) alors que l'action qui la provoque (le flash raté) a lieu juste avant, pendant un silence de plus de 30 s : on ne voit que le regret, pas le gameplay. Ajouter [moments] lead_in_seconds (CONFIG_DEFAULTS, défaut 0 = comportement actuel inchangé) : après sélection, le début de chaque moment recule de lead_in_seconds, sans jamais entrer dans une phrase précédente (borne = fin de la phrase précédente + petite marge), sans passer sous 0, et sans dépasser la durée maximale de la grille (si le recul ferait dépasser single_max / part_max, il est réduit d'autant ; jamais de fin raccourcie). Le recul appliqué est écrit dans le moment (lead_in_s) et journalisé. Les moments multi-parties : seulement le début de la partie 1. Tests : silence avant -> recul complet ; phrase 2 s avant -> recul borné à sa fin ; durée max atteinte -> recul réduit ; défaut 0 -> identique à avant. Aucun nom réel, aucun réseau, FakeBackend.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---
