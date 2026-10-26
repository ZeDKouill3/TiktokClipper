---
id: TASK-3d9b7fd7edad
type: task
slug: tiktok-roue-de-l-heure-le-clic-sur-une-minute-es
title: "TikTok roue de l'heure : le clic sur une minute est intercepté par le panneau (pointer events), publication arrêtée"
created: 2026-10-26T22:46:37Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/tiktok.py
  - clipper/assets/tiktok_selectors.toml
  - tests/test_tiktok.py
  - tests/fixtures
blocked_by: []
done_criteria: |
  Tests sans réseau sur la page réelle state/browser/17c3783183e1/captures/20261026T224226-unexpected_page.html (copiée en fixture ; roue ouverte, champ 09:30) : le choix de l'heure et de la minute dans la roue (pick_wheel_time) ne dépend plus d'un clic souris que le panneau peut intercepter (élément amené au centre de sa colonne avant clic, ou activation fiable équivalente, sans force ni contournement d'un contrôle de TikTok) ; si la valeur voulue est déjà sélectionnée (aria-selected=true) rien n'est cliqué ; la minute visée est toujours celle de la demande arrondie (journalisée), jamais une autre ; la valeur relue Picker__value doit valoir HH:MM attendu sinon arrêt explicite ; un test reproduit l'échec du 26/10 (minute 45 cliquée alors que 09:30 était demandé : trouver pourquoi, le corriger) ; ancienne page, calendrier et roue (TASK-adb1e4ae33fb) restent verts ; suite TikTok verte puis suite complète verte.
criteria_by: creator
verify: [tests]
proof:
  - type: test
    ref: local/a6567433e941@9b80d82
    tree: scope/2b949435d321
    criteria: 5e97b7170ec0
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---

Essai réel 26/10 22:43 UTC ClippTVFR, publication v2910270144/01 créneau 27/10 09:30 Paris : date OK, la capture PNG montre le champ « 09:30 » et la roue ouverte (09 et 30 au centre), mais le flux a tenté de cliquer button[role=option] aria-label « Minutes 45 » : Playwright « <div id=':r6s:' data-floating-ui-portal> subtree intercepts pointer events », ElementHandle.click timeout 10000 ms, arrêt « page inattendue ». Captures absolues : E:\ClaudeRandom\TiktokParseUpload\state\browser\17c3783183e1\captures\20261026T224226-unexpected_page.{html,png}. Pistes : la roue est une liste défilante (ScrollList) dont les éléments hors du centre sont couverts ; la minute visée 45 au lieu de 30 suggère que les minutes offertes ont été relues avant que l'heure 09 soit prise (minutes de l'heure courante 23 h : 45/50/55 seules dégrisées) ; vérifier l'ordre clic heure → attente → relecture. Aucun relevé réel par le worker : l'orchestrateur fait l'essai réel après fusion. Urgent : premier créneau ClippTVFR 27/10 09:30 Paris.
