---
id: TASK-f90efe186b9a
type: task
slug: worker-relire-config-toml-pour-le-plan-de-r-part
title: "Worker : relire config.toml pour le plan de répartition"
created: 2026-10-10T21:14:31Z
author: w-abc31eb1723e
status: open
scope:
  - clipper/worker.py
  - tests/test_worker.py
blocked_by: []
done_criteria: |
  Le worker relit config.toml (mtime) avant repartition.run_if_due : une source ajoutée à [repartition] excluded_sources est vue par le plan du soir sans redémarrage ; test de régression rouge avant, vert après.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---

Découvert par TASK-abc31eb1723e (audit 10/10 lot C, stats « config jamais rechargée ») : Worker._repartition_due passe self.config lu une seule fois.
