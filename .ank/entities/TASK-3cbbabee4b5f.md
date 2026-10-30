---
id: TASK-3cbbabee4b5f
type: task
slug: stats-youtube-relever-toutes-les-pages-de-conten
title: "Stats YouTube : relever toutes les pages de Contenus > Shorts (plus de 30 Shorts)"
created: 2026-10-30T07:56:17Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/youtube.py
blocked_by: []
done_criteria: |
  Le relevé des stats YouTube lit toutes les pages de la liste Contenus > Shorts (pied « 1 à N sur T » avec T > N) au lieu de s'arrêter en unexpected_page ; chaque page est vérifiée (lignes = plage du pied), aucune liste partielle silencieuse ; tests unitaires avec les fakes (2 pages, 3 pages, bouton page suivante absent = arrêt explicite) ; pytest complet passe.
criteria_by: creator
verify: [tests]
method: tdd
proof:
  - type: test
    ref: local/66fa97eebbb1@ba490ba
    tree: scope/62dbf474ca96
    criteria: c8506a22b66f
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---

Relevé réel 30/10 08:54 (Paris) sur la chaîne Bizarrium 670ca85bf1f0 : « relevé du compte 670ca85bf1f0 arrêté : 38 Shorts dans la chaîne mais 30 affichés : la pagination de la liste n'est pas gérée » (clipper/youtube.py shorts_rows, ~l.1060). Capture : state/browser/670ca85bf1f0/captures/20261030T075406-unexpected_page.png (dépôt principal, png seulement).

À faire :
- Parcourir les pages : bouton « page suivante » du pied de tableau de YouTube Studio (dans ytcp-table-footer, icône id « navigate-after », aria-label « Page suivante » en FR — à mettre dans clipper/assets/youtube_selectors.toml, pas en dur), attendre que le pied passe à « N+1 à M sur T » et que le nombre de lignes corresponde, lire les lignes de la page, recommencer jusqu'à M = T. Les handles de lignes d'une page deviennent invalides après le changement de page : lire chaque page (read_row) avant de passer à la suivante (adapter shorts_rows et son appelant).
- Alternative acceptable si plus simple : régler « Lignes par page » à 50 d'abord, mais il faut quand même gérer T > 50 (paginer).
- Si le bouton est absent ou le pied ne bouge pas : arrêt explicite (stop element_missing / unexpected_page), jamais une liste partielle (ADR-ad2e).
- Garder le reste du comportement identique ; tests dans tests/test_youtube.py avec les fakes existants (FakeUpload / fakes de stats).

Interdit : piloter un vrai navigateur ou toucher state/ / config.toml. MAIN fait le test réel après fusion ; si le sélecteur réel est incertain, le dire dans le rapport.
