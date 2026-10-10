---
id: TASK-abc31eb1723e
type: task
slug: audit-lot-c-validation-du-plan-exclusions-rejou
title: "Audit lot C : validation du plan = exclusions rejouées"
created: 2026-10-10T18:25:21Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/repartition.py
  - clipper/web/app.py
  - tests/test_repartition.py
  - tests/test_web.py
blocked_by: [TASK-14699025fdd6]
done_criteria: |
  Lot C de l'audit complet du 10/10 (contre-vérifié par Fable). Défauts couverts : publication-I3, stats-M2, stats « config jamais rechargée ». Détails, scénarios, preuves rejouables (scripts dans scratch-<domaine>/) : rapports E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\<domaine>.md (coeur, publication, web, jury, image, stats, media, veille, installeur ; id = <domaine>-I<n>/M<n>) et E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\contre-verif.md (verdicts, lot C) ; LECTURE SEULE, ne rien écrire dans research/. Correctif attendu : `clipper/web/app.py` (`validate_repartition` : `line_error` par ligne → 409 ; `_rep_view` : refus fusionnés, lignes `created` marquées), `clipper/repartition.py` (`run_if_due` relit `load_config()` quand aucune config n'est injectée, ou mtime), `tests/test_web.py`, `tests/test_repartition.py`. Critère CPU : Une source ajoutée à `excluded_sources` après le calcul : GET montre la ligne refusée, POST validate répond 409 sans créer d'entrée, et le coureur du plan voit la nouvelle liste sans redémarrage. Pour chaque défaut : test de régression ROUGE avant le correctif, vert après ; tests existants des modules touchés verts ; aucune valeur de secours silencieuse (ADR-ad2e) ; si un défaut s'avère faux ou exige une décision humaine (amendement de spec/ADR), le dire dans ank log et ne pas le corriger.
criteria_by: creator
verify: [tests]
method: diagnose
proof:
  - type: test
    ref: local/e96ef4a93005@47e15e2
    tree: scope/17bc8c53b8b7
    criteria: 72c9114c439d
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---
