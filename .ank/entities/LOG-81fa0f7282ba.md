---
id: LOG-81fa0f7282ba
type: log
title: "Relecture clause par clause : 1) default_http réessaie 4 types d'erreur, attempts/pause_s câblés"
created: 2026-11-19T08:16:33Z
author: w-fc2f36ec0889
scope:
  - clipper/veille_sources.py
  - clipper/veille.py
  - tests/test_veille_sources.py
  - tests/test_veille.py
  - CHANGELOG.md
about: TASK-fc2f36ec0889
seq: 6
schema: 4
version: 1
---

 depuis [veille] via _client, défauts 3/2.0 validés ; 2) WARNING par essai rattrapé (hôte, n° d'essai), dernier échec = SourceError inchangée ; 3) HTTP 429/500/503 non réessayés, testé ; 4) tests sans réseau (httpx.request simulé, time.sleep simulé), CHANGELOG ajouté. Lancement ank done (suite complète) par ank-done.ps1.
