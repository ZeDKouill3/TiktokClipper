---
id: TASK-5ee96c6bb385
type: task
slug: worker-et-serve-plus-de-mort-silencieuse-boucle
title: "Worker et serve : plus de mort silencieuse (boucle protégée, crochets d'exception, lignes démarré/arrêté)"
created: 2026-11-14T07:56:24Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/worker.py
  - clipper/__main__.py
  - clipper/journal.py
  - tests/test_worker.py
  - tests/test_main.py
  - tests/test_journal.py
  - CHANGELOG.md
blocked_by: []
done_criteria: |
  1) Worker.loop : une exception quelconque levée par tick() est journalisée avec sa trace (log.exception) et la boucle continue ; après N échecs consécutifs (réglage [worker] dans CONFIG_DEFAULTS, défaut 20) le worker sort avec un message explicite et un code non nul. 2) startup() : _recover_orphans et _recover_prefetch gardés comme les autres _recover_*. 3) _beat : toute OSError est journalisée une fois par message sans tuer la boucle. 4) serve et worker : sys.excepthook et threading.excepthook journalisent en critique avec la trace ; une ligne de journal « démarré pid=… » au lancement et « arrêté (raison ou code) » à la sortie, y compris sur Ctrl+C et quand uvicorn.run lève. 5) _terminate_pid : le second kill après la grâce revérifie l'identité du processus (process_alive(pid, created_at)) avant de tuer. 6) Tests unitaires pour chaque point (un tick qui lève ne tue pas la boucle ; N échecs sortent ; crochets posés ; lignes démarré/arrêté ; pid réattribué non tué). pytest passe en entier.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---

Revue r-publication du 14/11 (research/reviews/publication.md dans le dépôt principal, sections C1 et M4 : lis-les, elles donnent les lignes exactes). Constat : serve et son worker meurent plusieurs fois par semaine sans aucune ligne dans le journal ; Worker.loop fait while True: tick() sans try, main n'attrape que WorkerError, aucune trace hors stderr. Aucune valeur de secours silencieuse (ADR-ad2e) : tout échec est journalisé. Ne pas toucher au chemin de publication (_publish_next, _publish_one) : une autre tâche s'en occupe. Interdits : state/, config.toml, navigateur, réseau.
