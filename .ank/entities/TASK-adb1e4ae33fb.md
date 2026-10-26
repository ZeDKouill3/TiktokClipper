---
id: TASK-adb1e4ae33fb
type: task
slug: tiktok-nouvelle-page-choisir-l-heure-dans-le-tim
title: "TikTok nouvelle page : choisir l'heure dans le TimePicker à deux colonnes (Hours / Minutes)"
created: 2026-10-26T20:38:47Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/tiktok.py
  - clipper/assets/tiktok_selectors.toml
  - tests/test_tiktok.py
  - tests/fixtures
blocked_by: []
done_criteria: |
  Tests sans réseau sur la page réelle research/tiktok-schedule-1026/20261026T203415-element_missing.html (liste des heures ouverte, copiée en fixture) : l'heure est cliquée dans la colonne listbox aria-label « Hours » (button role=option aria-label « Hours HH », data-disabled=false), la minute la plus proche dans la colonne « Minutes » (pas affiché par TikTok, minute arrondie journalisée comme aujourd'hui), jamais un élément grisé ; la valeur relue dans Picker__value doit valoir HH:MM attendu sinon arrêt ; l'ancienne page et le calendrier (TASK-79f826c7b2b0) restent verts ; suite complète verte.
criteria_by: creator
verify: [tests]
schema: 4
version: 1
---

Suite de TASK-057adc52bd18 (ab05bd4) et TASK-79f826c7b2b0 (b109070). 2e essai réel 26/10 20:34 UTC sur ClippTVFR : la date passe (calendrier), l'heure s'arrête comme prévu : « aucun choix reconnu ([role='option']) » car time_minute attend des textes « HH:MM ». DOM relevé : div.Picker__panel.Dropdown__content > div.TimePicker__panel--columns-two > div[role=group][aria-label='Time selection'].TimePicker__wheel > deux div[role=listbox].TimePicker__column, aria-label « Hours » (boutons « 00 »..« 23 », aria-label « Hours 07 ») puis la colonne minutes (vérifier son aria-label et son pas dans le HTML). Boutons : button[role=option].TimePicker__item aria-selected/data-selected/data-disabled. Roue avec ScrollList : un bouton hors vue peut demander scroll_into_view avant clic. Ce panneau n'a PAS la classe schedule-picker-panel (contrairement au calendrier) : vérifier qu'il n'est pas pris pour une fenêtre surgissante. Capture absolue : E:\ClaudeRandom\TiktokParseUpload\research\tiktok-schedule-1026\20261026T203415-element_missing.{html,png}. Aucun relevé réel par le worker.
