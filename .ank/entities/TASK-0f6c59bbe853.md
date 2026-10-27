---
id: TASK-0f6c59bbe853
type: task
slug: youtube-programmer-un-short-avec-la-date-en-list
title: "YouTube : programmer un Short avec la date en liste déroulante"
created: 2026-10-26T11:45:49Z
author: nicoc@zedk_ordi
status: in_progress
scope:
  - clipper/youtube.py
  - clipper/assets/youtube_selectors.toml
  - tests/test_youtube.py
  - tests/test_youtube_restored_tabs.py
blocked_by: []
done_criteria: |
  Preuve réelle : un Short programmé de bout en bout sur la chaîne 670ca85bf1f0 (date et heure de Paris relues dans Studio après coup), avec la liste des brouillons de test créés à supprimer.
criteria_by: creator
verify: [tests]
schema: 4
version: 8
---

Constat 26/10 (relevé réel, chaîne 670ca85bf1f0) : dans la dialog d'envoi de YouTube Studio (français), section Visibilité > « Programmer », après le chevron #second-container-expand-button (corrigé cf29727), la DATE est désormais une liste déroulante (« 27 oct. 2026 ⌄ »), l'HEURE un champ « 00:00 », le FUSEAU une liste « (UTC+01:00) P… ». `_Flow.schedule_fields` cherche deux `input.tp-yt-paper-input` reconnus par leur valeur et échoue : « champs de programmation inattendus ». Capture : state/browser/670ca85bf1f0/captures/20261026T112142-unexpected_page.png. La publication immédiate fonctionne (0934e08, f8f0f76).

En attendant, ai-video/yt_mirror.py (pane herdr yt-mirror) publie en immédiat à l'heure TikTok ; une programmation qui marche permettrait d'éteindre le PC.

Relevé réel autorisé : profil Clipper 670ca85bf1f0 via clipper.browser._open_context (headless=False, verrou de pilotage partagé avec le worker et le miroir). Chaque essai d'envoi laisse un brouillon : utiliser un mp4 de test court (3 s, 1080x1920) titré « TEST relevé à supprimer », et lister dans le rapport chaque brouillon créé.
