---
id: TASK-5b0bef2e6edd
type: task
slug: audit-lot-e-parts-accroche-et-moments-d-action
title: "Audit lot E : parts : accroche et moments d'action"
created: 2026-10-10T18:25:22Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/parts.py
  - tests/test_parts.py
blocked_by: []
done_criteria: |
  Lot E de l'audit complet du 10/10 (contre-vérifié par Fable). Défauts couverts : jury-I1, jury-I2. Détails, scénarios, preuves rejouables (scripts dans scratch-<domaine>/) : rapports E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\<domaine>.md (coeur, publication, web, jury, image, stats, media, veille, installeur ; id = <domaine>-I<n>/M<n>) et E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\contre-verif.md (verdicts, lot E) ; LECTURE SEULE, ne rien écrire dans research/. Correctif attendu : `clipper/parts.py` (`_inside` inclut la phrase qui contient `start` ; `source == "action"` ou single sans phrase → partie unique avec `hook_text` du moment), `tests/test_parts.py`. Critère CPU : Un moment `source: action` sans phrase et un moment commençant après un connecteur donnent chacun une partie 1 dont `hook_text` = celui de moments.json, 0 rejet. Pour chaque défaut : test de régression ROUGE avant le correctif, vert après ; tests existants des modules touchés verts ; aucune valeur de secours silencieuse (ADR-ad2e) ; si un défaut s'avère faux ou exige une décision humaine (amendement de spec/ADR), le dire dans ank log et ne pas le corriger.
criteria_by: creator
verify: [tests]
method: diagnose
proof:
  - type: test
    ref: local/1ed57fcff03e@31d42ef
    tree: scope/edd47dd5da1e
    criteria: 4b188e53017b
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---
