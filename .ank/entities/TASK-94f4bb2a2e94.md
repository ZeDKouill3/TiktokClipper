---
id: TASK-94f4bb2a2e94
type: task
slug: audit-lot-p-console-vid-o-jamais-retir-e-sur-500
title: "Audit lot P : console : vidéo jamais retirée sur 500"
created: 2026-10-10T18:25:26Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/web/static/app.js
  - tests/test_web.py
blocked_by: [TASK-a1966b0b3ab7]
done_criteria: |
  Lot P de l'audit complet du 10/10 (contre-vérifié par Fable). Défauts couverts : web-I5. Détails, scénarios, preuves rejouables (scripts dans scratch-<domaine>/) : rapports E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\<domaine>.md (coeur, publication, web, jury, image, stats, media, veille, installeur ; id = <domaine>-I<n>/M<n>) et E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\contre-verif.md (verdicts, lot P) ; LECTURE SEULE, ne rien écrire dans research/. Correctif attendu : `clipper/web/static/app.js` (`api` expose `status`, `reloadVideo` ne retire que sur 404 + `toastError`), `tests/test_web.py` (test node existant étendu). Critère CPU : Sur une erreur 500 la vidéo reste dans le magasin et un toast est émis ; sur 404 elle est retirée. Pour chaque défaut : test de régression ROUGE avant le correctif, vert après ; tests existants des modules touchés verts ; aucune valeur de secours silencieuse (ADR-ad2e) ; si un défaut s'avère faux ou exige une décision humaine (amendement de spec/ADR), le dire dans ank log et ne pas le corriger.
criteria_by: creator
verify: [tests]
method: diagnose
proof:
  - type: test
    ref: local/969b3786f3e1@5c3864d
    tree: scope/c3bc290994d4
    criteria: 93017d477176
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---
