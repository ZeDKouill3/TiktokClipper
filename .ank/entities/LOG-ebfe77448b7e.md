---
id: LOG-ebfe77448b7e
type: log
title: "instrumentation (trace existence du .tmp pendant la relecture) : web relit existe=True (debut) et"
created: 2026-10-10T19:36:30Z
author: w-5856ff596c38
scope:
  - clipper/channel.py
  - clipper/config.py
  - tests/test_channel.py
  - tests/test_config.py
about: TASK-5856ff596c38
seq: 4
schema: 4
version: 1
---

 apres 0.4 s; worker relit existe=True (debut) puis existe=False (apres 0.4 s) -> ConfigError. Mecanisme confirme : le os.replace de web consomme le .tmp du worker.
