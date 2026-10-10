---
id: TASK-5a1fa4e5732d
type: task
slug: audit-lot-a3-reprise-des-queued-par-un-enfant
title: "Audit lot A3 : reprise des `queued` par un enfant"
created: 2026-10-10T18:25:20Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/__main__.py
  - clipper/pipeline.py
  - clipper/worker.py
  - tests/test_pipeline_state.py
  - tests/test_worker.py
  - tests/test_web.py
blocked_by: [TASK-3a12bbabc977]
done_criteria: |
  Lot A3 de l'audit complet du 10/10 (contre-vérifié par Fable). Défauts couverts : coeur-I5, coeur-M3 (disparaît). Détails, scénarios, preuves rejouables (scripts dans scratch-<domaine>/) : rapports E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\<domaine>.md (coeur, publication, web, jury, image, stats, media, veille, installeur ; id = <domaine>-I<n>/M<n>) et E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\contre-verif.md (verdicts, lot A3) ; LECTURE SEULE, ne rien écrire dans research/. Correctif attendu : `clipper/worker.py` (`tick` : entrée de file `action=run/render` au lieu de `process_queue` inline, suppression de `busy`/`_worker_busy_inline`), `clipper/pipeline.py` (`attempts` non remis à zéro, option `--resume`), `clipper/__main__.py`, `tests/test_worker.py`, `tests/test_pipeline_state.py`, SPEC-74e9 §2.4 (amendement humain). Critère CPU : Une vidéo `queued` dont `retry_at` est passé produit une entrée de file lancée par `_launch_head` (FakeSpawner) ; pendant son « enfant », `tick()` appelle encore `_publish_due` et `cancel` la termine. Pour chaque défaut : test de régression ROUGE avant le correctif, vert après ; tests existants des modules touchés verts ; aucune valeur de secours silencieuse (ADR-ad2e) ; si un défaut s'avère faux ou exige une décision humaine (amendement de spec/ADR), le dire dans ank log et ne pas le corriger.
criteria_by: creator
verify: [tests]
method: diagnose
proof:
  - type: test
    ref: local/0b1f8e5a8f39@8c275a1
    tree: scope/ef133d7aee7c
    criteria: fd3e09e00b14
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 4
---
