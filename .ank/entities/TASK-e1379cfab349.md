---
id: TASK-e1379cfab349
type: task
slug: reframe-cran-d-attente-dessin-sur-plusieurs-p-ri
title: "Reframe : écran d'attente dessiné sur plusieurs périodes -> ReframeError « plusieurs candidats visage stables et persistants » alors que Claude répond à raison aucune webcam"
created: 2026-10-11T06:19:34Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/reframe.py
  - tests/test_reframe.py
blocked_by: []
done_criteria: |
  Cas réel : workspace/v2895819258 (VOD gaming, planche facecam/period_0.jpg = écran d'attente illustré « J'arrive » avec nuages et mascotte ; candidats 4 et 7 = zones de nuages fixes, persistants sur toutes les périodes car l'écran d'attente ne bouge pas). _stable_face_over_null lève ReframeError dès que plus d'un candidat stable et persistant existe. Diagnostiquer : combien de périodes, pourquoi l'écran d'attente occupe toutes les périodes (le stream réel commence-t-il plus tard ? les périodes couvrent-elles la VOD entière ?), et si face_support de nuages dessinés est plausible. Corriger sans assouplir SPEC-b19b ni ADR-ad2e : soit les périodes doivent couvrir le stream réel (écran d'attente exclu), soit des candidats persistants mais identiques pixel à pixel d'une période à l'autre (décor fixe, pas une webcam vivante) ne contredisent pas Claude. Test unitaire reproduisant le cas (fixtures synthétiques, aucun nom réel, aucun réseau, FakeBackend). Relance réelle de v2895819258 depuis reframe faite par l'orchestrateur, pas par la tâche.
criteria_by: creator
verify: [tests]
method: diagnose
proof:
  - type: test
    ref: local/a455def8c8e3@98e28e6
    tree: scope/06865911774f
    criteria: 3efc2ade0318
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---
