---
id: TASK-3a12bbabc977
type: task
slug: audit-lot-a2-arbre-de-processus
title: "Audit lot A2 : arbre de processus"
created: 2026-10-10T18:25:20Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/__main__.py
  - clipper/worker.py
  - tests/test_worker.py
blocked_by: [TASK-4ca998e97789]
done_criteria: |
  Lot A2 de l'audit complet du 10/10 (contre-vérifié par Fable). Défauts couverts : coeur-I6/image-I2. Détails, scénarios, preuves rejouables (scripts dans scratch-<domaine>/) : rapports E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\<domaine>.md (coeur, publication, web, jury, image, stats, media, veille, installeur ; id = <domaine>-I<n>/M<n>) et E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\contre-verif.md (verdicts, lot A2) ; LECTURE SEULE, ne rien écrire dans research/. Correctif attendu : `clipper/worker.py` (`_popen_logged`, `_spawn_prefetch`, `_terminate_pid` : Job Object `KILL_ON_JOB_CLOSE` ou `taskkill /T`), `clipper/__main__.py` (serve → worker), `tests/test_worker.py`. Critère CPU : Un enfant python qui lance un petit-enfant `python -c sleep` : après `cancel`, les deux pids sont morts en moins de `cancel_grace_s`. Pour chaque défaut : test de régression ROUGE avant le correctif, vert après ; tests existants des modules touchés verts ; aucune valeur de secours silencieuse (ADR-ad2e) ; si un défaut s'avère faux ou exige une décision humaine (amendement de spec/ADR), le dire dans ank log et ne pas le corriger.
criteria_by: creator
verify: [tests]
method: diagnose
proof:
  - type: test
    ref: local/445244434502@b63e524
    tree: scope/05e65db88f87
    criteria: a130055974b9
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---
