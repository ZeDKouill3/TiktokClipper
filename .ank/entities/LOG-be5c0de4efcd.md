---
id: LOG-be5c0de4efcd
type: log
title: "Mesure : la suppression du battement busy casse 3 tests de tests/test_web.py (fixture _busy_worker"
created: 2026-10-10T22:16:48Z
author: w-5a1fa4e5732d
scope:
  - clipper/__main__.py
  - clipper/pipeline.py
  - clipper/worker.py
  - tests/test_pipeline_state.py
  - tests/test_worker.py
  - tests/test_web.py
about: TASK-5a1fa4e5732d
seq: 6
schema: 4
version: 1
---

 ecrit worker.json busy=true pour que leurs videos running sans entree de file ne soient pas « interrupted ») : status 'interrupted' != 'running'. Le comportement produit est le bon (toute video en cours a desormais son entree de file) ; c'est la fixture qui est perimee. Scope etendu a tests/test_web.py (ank amend --scope) pour remplacer _busy_worker par une entree running vivante dans state/queue.json ; TASK-a1966 (lot D) tient aussi ce fichier, ma retouche se limite a l'aide en tete de fichier (lignes 44-48).
