---
id: LOG-67b41e1ae68a
type: log
title: Reproduit sur ce worktree (repros audit copies dans le scratchpad, process_queue remplace par 1,5
created: 2026-10-10T22:04:22Z
author: w-5a1fa4e5732d
scope:
  - clipper/__main__.py
  - clipper/pipeline.py
  - clipper/worker.py
  - tests/test_pipeline_state.py
  - tests/test_worker.py
about: TASK-5a1fa4e5732d
seq: 3
schema: 4
version: 1
---

 s) : coeur-I5 -> « cancel(A) refuse -> aucune video en cours », battement « stale », tick 1.6 s, 0 appel _publish_due pendant la reprise. coeur-M3 -> process_queue avec [watch] presets_dir=tmp/presets et cwd ailleurs : « chaine 'ma_chaine' inutilisable a la reprise : chaine inconnue ». Cause I5 : worker.tick -> pipeline.process_queue en ligne (worker.py ~879-890), pas d'entree de file ; cause M3 : pipeline._channel_config -> load_channel(name) sans presets_dir/base. SPEC-74e9 §2.4 (« sans passer par state/queue.json ») contredit ADR-35b7 §1 (enfant python -m clipper que l'annulation termine) : amendement de la spec = decision humaine, hors de ce lot ; le critere frozen impose le passage par la file, je code le correctif et laisse §2.4 a l'humain.
