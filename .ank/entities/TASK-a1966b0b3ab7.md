---
id: TASK-a1966b0b3ab7
type: task
slug: audit-lot-d-garde-locale-de-l-api
title: "Audit lot D : garde locale de l'API"
created: 2026-10-10T18:25:21Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/web/app.py
  - tests/test_web.py
blocked_by: [TASK-abc31eb1723e]
done_criteria: |
  Lot D de l'audit complet du 10/10 (contre-vérifié par Fable). Défauts couverts : web-I1, web-I2, web-M3, web-M1, web-M2. Détails, scénarios, preuves rejouables (scripts dans scratch-<domaine>/) : rapports E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\<domaine>.md (coeur, publication, web, jury, image, stats, media, veille, installeur ; id = <domaine>-I<n>/M<n>) et E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\contre-verif.md (verdicts, lot D) ; LECTURE SEULE, ne rien écrire dans research/. Correctif attendu : `clipper/web/app.py` (`_check_token` : Host local + `Origin`/`Sec-Fetch-Site` sur méthodes modifiantes, `[::1]` ; `_validate_video_id` sur les 4 routes ; `_list_states` tolérant ; `_stats_bound` Paris), `tests/test_web.py`. Critère CPU : `POST /api/clips/{v}/{c}/reject` avec `Origin: https://evil.test` → 403 ; `GET /api/videos/..%5Cx/clips` → 400 ; un `pipeline.json` vide n'empêche pas `GET /api/videos`. Pour chaque défaut : test de régression ROUGE avant le correctif, vert après ; tests existants des modules touchés verts ; aucune valeur de secours silencieuse (ADR-ad2e) ; si un défaut s'avère faux ou exige une décision humaine (amendement de spec/ADR), le dire dans ank log et ne pas le corriger.
criteria_by: creator
verify: [tests]
method: diagnose
proof:
  - type: test
    ref: local/0254ea56d671@f9de134
    tree: scope/072815181533
    criteria: 5069cf1a7526
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---
