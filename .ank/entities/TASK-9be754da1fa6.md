---
id: TASK-9be754da1fa6
type: task
slug: tests-reframe-et-qa-fixtures-r-solution-r-duite
title: "Tests reframe et qa : fixtures à résolution réduite là où la résolution n'est pas l'objet du test"
created: 2026-10-11T15:24:29Z
author: nicoc@zedk_ordi
status: closed
scope:
  - tests/test_reframe.py
  - tests/test_qa.py
  - tests/conftest.py
blocked_by: []
done_criteria: |
  Mesure du 11/10 (suite -n 3 : 657-692 s) : test_reframe pèse 525 s (195 tests) parce que ses images de synthèse sont en 1080p et passent par les vrais calculs (imencode, Sobel, imread, _edge_mask) ; test_qa 240 s (ffmpeg blackdetect sur des mp4 générés). Réduire la taille des images et vidéos de fixture (ex. 480x270 ou 640x360, durées minimales) dans les tests où la résolution n'est PAS ce qui est vérifié, en gardant en 1080p ceux qui testent des seuils en pixels absolus (les lister dans ank log). Aucun test retiré ni affaibli, aucun changement de code de production. Critère : test_reframe + test_qa au moins 2x plus rapides (mesure avant/après -n 3 consignée dans ank log), suite complète verte.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 2
---
