---
id: LOG-13b6ebb6e252
type: log
title: "reproduction : scratch-web/race_write_config.py non modifie (sleep 0.4 sur relecture .tmp) ->"
created: 2026-10-10T19:36:21Z
author: w-5856ff596c38
scope:
  - clipper/channel.py
  - clipper/config.py
  - tests/test_channel.py
  - tests/test_config.py
about: TASK-5856ff596c38
seq: 2
schema: 4
version: 1
---

 erreurs {worker: ConfigError fichier de config introuvable ...ma_chaine.toml.tmp}, contenu final {mode auto, display_name avant} (modif console perdue), tmp restant []. Sans sleep : 0 erreur sur 3 passages (ecritures separees de 150 ms).
