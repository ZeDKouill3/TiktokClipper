---
id: TASK-cee612eefa16
type: task
slug: audit-lot-g-reframe-p-riode-unique
title: "Audit lot G : reframe : période unique"
created: 2026-10-10T18:25:22Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/reframe.py
  - tests/test_reframe.py
blocked_by: []
done_criteria: |
  Lot G de l'audit complet du 10/10 (contre-vérifié par Fable). Défauts couverts : image-I1, image-M2, image-M5. Détails, scénarios, preuves rejouables (scripts dans scratch-<domaine>/) : rapports E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\<domaine>.md (coeur, publication, web, jury, image, stats, media, veille, installeur ; id = <domaine>-I<n>/M<n>) et E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\contre-verif.md (verdicts, lot G) ; LECTURE SEULE, ne rien écrire dans research/. Correctif attendu : `clipper/reframe.py` (`_stable_face_over_null` : jamais d'override sur `len(periods) <= 1` ; `_size_camera_rect`/tolérance d'arrondi ; vidage de `facecam/`), `tests/test_reframe.py` (test 2948 inversé). Critère CPU : Avec une seule période et un seul candidat stable, la réponse `null` de Claude est conservée et journalisée ; un rectangle 112×78 n'est plus refusé par `_reframe_stream`. Pour chaque défaut : test de régression ROUGE avant le correctif, vert après ; tests existants des modules touchés verts ; aucune valeur de secours silencieuse (ADR-ad2e) ; si un défaut s'avère faux ou exige une décision humaine (amendement de spec/ADR), le dire dans ank log et ne pas le corriger.
criteria_by: creator
verify: [tests]
method: diagnose
proof:
  - type: test
    ref: local/1f130a18aaf0@dd49e7d
    tree: scope/6fe461bb31cb
    criteria: c83f7530f3ed
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---
