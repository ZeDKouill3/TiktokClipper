---
id: LOG-7d8cb78c5e02
type: log
title: "I1 : tests rouges avant correctif (TypeError config_base, KeyError config_path), verts apres :"
created: 2026-10-11T03:57:49Z
author: w-d850f9d78db2
scope:
  - clipper/worker.py
  - clipper/__main__.py
  - tests/test_worker.py
  - tests/test_cli.py
about: TASK-d850f9d78db2
seq: 2
schema: 4
version: 1
---

 Worker(config_path, config_base), mtime tuple fichier+base, load_config(chemin, base=base) ; __main__ worker passe args.config + base config.toml. test_cli/test_worker/test_web 1160 verts.
