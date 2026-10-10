---
id: LOG-b69e8dd7f54b
type: log
title: "Tests de regression ecrits et ROUGES (13) : tests/test_worker.py (tick -> entree de file + enfant"
created: 2026-10-10T22:08:50Z
author: w-5a1fa4e5732d
scope:
  - clipper/__main__.py
  - clipper/pipeline.py
  - clipper/worker.py
  - tests/test_pipeline_state.py
  - tests/test_worker.py
about: TASK-5a1fa4e5732d
seq: 4
schema: 4
version: 1
---

 run/render --resume, _publish_due pendant l'enfant, cancel le termine, retry_at futur ignore, pas de doublon, attend derriere les waiting, chaine -> --config presets, chaine disparue -> reason visible une fois, source_url absent -> erreur visible, battement busy ne masque plus une interrompue, --resume -> manual=False via la vraie CLI) ; tests/test_pipeline_state.py (process_queue lit [watch] presets_dir/base_config, run/render manual=False gardent attempts). Commande : pytest -n0 tests/test_worker.py tests/test_pipeline_state.py -k 'queued_video or resume_flag or resumed_child or busy_heartbeat or presets_dir_of_the_config or manual_run_resets' -> 13 failed.
