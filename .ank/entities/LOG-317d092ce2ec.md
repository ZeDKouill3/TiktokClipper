---
id: LOG-317d092ce2ec
type: log
title: "Mesure: test_a_to_verify_failure_is_refused_by_every_action... [set_account] ROUGE avant correctif"
created: 2026-10-11T06:18:03Z
author: w-0c82634a1b15
scope:
  - clipper/publish.py
  - tests/test_publish.py
about: TASK-0c82634a1b15
seq: 2
schema: 4
version: 1
---

 : set_account sur entree failed+to_verify ne leve pas (DID NOT RAISE). Les 5 autres actions passent. Cause: set_account (clipper/publish.py ~l.977) n'appelle pas _refuse_to_verify, contrairement a move/unschedule/update_post/cancel_post/set_mode.
