---
id: TASK-0deb90a88c0c
type: task
slug: publication-une-coupure-r-seau-r-veil-du-pc-ne-d
title: "Publication : une coupure réseau (réveil du PC) ne décoche plus le compte"
created: 2026-11-19T08:08:05Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/worker.py
  - clipper/tiktok.py
  - clipper/youtube.py
  - clipper/browser.py
  - clipper/accounts.py
  - tests/test_worker.py
  - tests/test_tiktok.py
  - tests/test_youtube.py
  - CHANGELOG.md
blocked_by: []
done_criteria: |
  1) Une tentative de publication (TikTok ou YouTube) qui échoue parce que le réseau est coupé (Playwright net::ERR_INTERNET_DISCONNECTED, ERR_NETWORK_CHANGED, ERR_NAME_NOT_RESOLVED, ERR_CONNECTION_*, ERR_TIMED_OUT ou ERR_ADDRESS_UNREACHABLE sur un Page.goto, avant tout envoi de fichier) n'est plus un arrêt R4 : le compte reste « prêt à publier », l'entrée reste scheduled avec une waiting_reason qui dit « réseau indisponible » et le détail, journalisée une seule fois par coupure, et elle est retentée aux tours suivants (les règles existantes du créneau dépassé s'appliquent). 2) Le navigateur fermé pendant le pilotage (TargetClosedError) AVANT le clic final de publication suit la même règle ; après ce clic, le comportement actuel (arrêt R4, vérification manuelle) est conservé : jamais de risque de doublon. 3) Un relevé de statistiques qui échoue pour la même raison réseau n'écrit pas d'arrêt qui bloque le relevé pendant stats_interval_h : il est retenté au plus tôt après un délai court borné (pas à chaque tour), et le journal le dit une fois. 4) Toute autre page inattendue garde exactement le comportement actuel (arrêt R4, capture, compte décoché). 5) Tests unitaires sans réseau pour chaque point (faux pilote qui lève ces erreurs), suite complète verte, ligne dans CHANGELOG.md.
criteria_by: creator
verify: [tests]
method: tdd
proof:
  - type: test
    ref: local/9f0342a99e4a@0917af5
    tree: scope/87d6b04f42f8
    criteria: adbe64f004b1
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---

Constat du 19/11/2026 (MAIN) : le PC est mis en veille chaque soir. Le 16/11 à 08:25 (heure de Paris) il se réveille, le worker tente aussitôt une publication alors que le Wi-Fi n'est pas encore revenu : « page inattendue : Error : Page.goto: net::ERR_INTERNET_DISCONNECTED at https://www.tiktok.com/tiktokstudio/upload » sur ClippTVFR, puis « TargetClosedError : Page.query_selector: Target page, context or browser has been closed » sur ClipperFou et TwitchClipperTV. Les 3 comptes sont décochés automatiquement (arrêt R4) et plus rien n'est parti jusqu'au 19/11 : 67 créneaux perdus. Même chose au relevé de stats (logs/journal-2026-11-18.log : ERR_NETWORK_CHANGED, ERR_INTERNET_DISCONNECTED, Timeout 30000ms sur tiktokstudio/analytics). Une coupure réseau n'est pas un problème de compte : elle doit mettre en attente avec une raison visible (ADR-ad2e), pas arrêter le compte. Lis comment worker._publish_one, tiktok.publish/TikTokStop et accounts (décochage automatique, r4_halt) traitent aujourd'hui « page inattendue », et où se situe le clic final dans tiktok.py / youtube.py. Ne change rien au traitement des vrais refus de la plateforme.
