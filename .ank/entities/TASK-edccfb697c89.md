---
id: TASK-edccfb697c89
type: task
slug: pr-parer-la-v0-7-0-changelog-notes-de-version-fe
title: "Préparer la v0.7.0 : CHANGELOG, notes de version, feuille de route, version, zip portable vérifié"
created: 2026-10-11T11:31:21Z
author: nicoc@zedk_ordi
status: open
scope:
  - CHANGELOG.md
  - docs/releases/**
  - docs/versions.md
  - pyproject.toml
  - README.md
  - tests/test_readme*.py
  - tests/test_changelog*.py
blocked_by: [TASK-60bddde86152, TASK-a0226252101a, TASK-999bc55655f6, TASK-61002cfd243f, TASK-38a299091bd4]
done_criteria: |
  v0.7.0 = console v3 (ADR-a308, SPEC-7715), corrections de l'audit complet du 10/10 et de la relecture du 11/10, répartition automatique du lendemain (SPEC-6d1f), boucle d'apprentissage branchée (ADR-c260, SPEC-040f), veille (historique 30 j, style par défaut), modération TikTok (termes à risque), styles gaming (amorce de gameplay, style combat), installeur SPEC-54ed. (1) CHANGELOG : la section [Non publié] devient [0.7.0] - <date>, complétée à partir de git log v0.6.0..HEAD (aucune fonctionnalité fusionnée oubliée, regroupée par Ajouté / Modifié / Corrigé, phrases courtes, aucun nom réel) ; nouvelle section [Non publié] vide. (2) docs/releases/v0.7.0.md : notes pour l'utilisateur (ce qui change à l'écran, ce qu'il faut faire en mettant à jour, risques connus). (3) docs/versions.md : ligne v0.7.0 publiée avec résumé ; la ligne « mode auto de bout en bout » passe à v0.8.0 si elle n'est pas tenue (une semaine d'usage réel sans retouche). (4) pyproject version = 0.7.0. (5) python tools/build_portable.py produit le zip dans dist/ ; suite complète verte (ank done). Le test réel de l'installeur (CLIPPER_INSTALLER_REAL=1) et la publication GitHub sont faits par l'orchestrateur après accord de l'utilisateur, pas par la tâche.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---
