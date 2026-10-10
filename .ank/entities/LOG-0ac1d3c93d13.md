---
id: LOG-0ac1d3c93d13
type: log
title: "Reproduit sur 31d42ef (scripts audit rejoues, python -B, research/ intact) : coeur-I1 (2 running"
created: 2026-10-10T18:33:34Z
author: w-4ca998e97789
scope:
  - clipper/__main__.py
  - clipper/publish.py
  - clipper/worker.py
  - tests/test_publish.py
  - tests/test_worker.py
about: TASK-4ca998e97789
seq: 2
schema: 4
version: 1
---

 apres tick, A perdu par move_to_front, A running pid mort a la fin), coeur-I2 (2 workers = 2 enfants, 1 pid de battement), coeur-I3 (cancel tue un python -c sleep etranger, code 15 ; is_interrupted False ; resume refuse), coeur-M1 (render/reframe restent running sous failed), coeur-M2 (reason = ANCIEN ECHEC apres crash code 1), publication-I2 + M1 (3 passed), web-I6 (render -wtIMTCHWuI -> argparse SystemExit 2). Cause commune I1/I2/I3 : identite d'un processus = existence du pid seulement, et aucune lecture de worker.json ni des running etrangeres au lancement.
