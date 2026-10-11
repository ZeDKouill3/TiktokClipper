---
id: TASK-d850f9d78db2
type: task
slug: worker-la-relecture-de-config-surveille-le-fichi
title: "Worker : la relecture de config surveille le fichier réellement chargé (--config + base)"
created: 2026-10-11T00:54:29Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/worker.py
  - clipper/__main__.py
  - tests/test_worker.py
  - tests/test_cli.py
blocked_by: []
done_criteria: |
  Relecture des fusions du 11/10, I1 : f90e surveille config.toml même quand serve/worker tournent avec --config X (base config.toml) ; un changement de X n'est jamais vu et toucher config.toml remplace la config du plan par config.toml seul. Correctif : __main__ passe à Worker le chemin chargé et sa base ; la relecture recharge load_config(chemin, base=base) et surveille le mtime des deux fichiers. Critère : test avec --config autre.toml (base config.toml) : modifier autre.toml est vu par le plan, toucher config.toml garde les clés de autre.toml. Détails, scénario et preuve rejouable : E:\ClaudeRandom\TiktokParseUpload\research\reviews\fusions-1011.md (section I1) et research/reviews/scratch-fusions-1011/ (lecture seule). Test de régression ROUGE avant le correctif, vert après ; tests existants des modules touchés verts ; aucune valeur de secours silencieuse (ADR-ad2e).
criteria_by: creator
verify: [tests]
method: diagnose
schema: 4
version: 1
---
