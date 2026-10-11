---
id: TASK-0e3be9c203f2
type: task
slug: download-un-fichier-jug-incomplet-est-ret-l-char
title: "Download : un fichier jugé incomplet est retéléchargé, message juste"
created: 2026-10-11T00:54:30Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/download.py
  - clipper/pipeline.py
  - tests/test_download.py
  - tests/test_pipeline.py
blocked_by: []
done_criteria: |
  Relecture des fusions du 11/10, I2 : un fichier refusé par le contrôle de durée (lot H) n'est jamais retéléchargé : l'étape reste bloquée avec un message qui dit de relancer download alors que la relance réutilise le même fichier. Correctif selon le rapport (fichier incomplet mis de côté/supprimé ou reprise forcée, message exact). Critère : un premier download produit un fichier trop court -> erreur ; la relance de l'étape retélécharge et réussit (faux téléchargeur injecté). Détails, scénario et preuve rejouable : E:\ClaudeRandom\TiktokParseUpload\research\reviews\fusions-1011.md (section I2) et research/reviews/scratch-fusions-1011/ (lecture seule). Test de régression ROUGE avant le correctif, vert après ; tests existants des modules touchés verts ; aucune valeur de secours silencieuse (ADR-ad2e).
criteria_by: creator
verify: [tests]
method: diagnose
schema: 4
version: 1
---
