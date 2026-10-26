---
id: TASK-057adc52bd18
type: task
slug: tiktok-programmer-avec-la-date-et-l-heure-en-lis
title: "TikTok : programmer avec la date et l'heure en listes déroulantes (nouvelle page Studio)"
created: 2026-10-26T15:59:07Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/tiktok.py
  - clipper/assets/tiktok_selectors.toml
  - tests/test_tiktok.py
  - tests/fixtures
blocked_by: []
done_criteria: |
  Tests (sans réseau) qui chargent la page réelle capturée research/tiktok-schedule-1026/20261026T155623-element_missing.html (copiée en fixture) et prouvent que schedule_later règle date + heure de Paris dans la nouvelle page (date « 26 oct. 2026 » et heure « 17:15 » en listes déroulantes, fuseau « (GMT+1) Heure locale »), relit la valeur affichée et échoue clairement si le fuseau affiché n'est pas celui de Paris ; l'ancienne page (deux input TUXTextInputCore) reste gérée ; suite complète verte.
criteria_by: creator
verify: [tests]
schema: 4
version: 1
---

Constat 26/10 (compte ClippTVFR 17c3783183e1, 3 échecs « élément attendu absent après 30 s : schedule_inputs » à 10:51, 15:56 UTC) : dans TikTok Studio > Paramètres > Quand publier > Programmer, la DATE est une liste déroulante « 26 oct. 2026 ⌄ », l'HEURE une liste « 17:15 ⌄ », plus un nouveau choix de FUSEAU « (GMT+1) Heure locale ⌄ ». Les deux input.TUXTextInputCore-input attendus par _Flow.schedule_later n'existent plus. Les autres comptes (ClipperFou, TwitchClipperTV, ClipManiaq3) programment encore avec l'ancienne page : déploiement progressif par compte, les deux pages doivent marcher. Capture + DOM : research/tiktok-schedule-1026/20261026T155623-element_missing.{png,html}. Aucun relevé réel par le worker (le pilote fera la preuve réelle sur ClippTVFR après fusion : un clip en attente relancé). Si le DOM capturé ne suffit pas (contenu des listes ouvertes absent), le dire dans le rapport : le pilote capturera la liste ouverte. Même famille que TASK-0f6c59bbe853 (YouTube).
