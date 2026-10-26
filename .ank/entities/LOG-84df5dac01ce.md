---
id: LOG-84df5dac01ce
type: log
title: "Releve réel 26/10 (profil 670ca85bf1f0, dialog FR) : après expand, #second-container reste visible"
created: 2026-10-26T12:26:53Z
author: w-0f6c59bbe853
scope:
  - clipper/youtube.py
  - clipper/assets/youtube_selectors.toml
  - tests/test_youtube.py
  - tests/test_youtube_restored_tabs.py
about: TASK-0f6c59bbe853
seq: 1
schema: 4
version: 1
---

 ; date = ytcp-text-dropdown-trigger#datepicker-trigger (span.dropdown-trigger-text « 27 oct. 2026 », ouvre un calendrier non encore relevé) ; heure = #time-of-day-container input (tp-yt-paper-input, valeur « 00:00 ») ; fuseau = ytcp-button#timezone-select-button (libellé fixe « Fuseau horaire », la zone n'est pas dans le texte du bouton : la relecture actuelle de schedule_later échouerait). Relevé du calendrier et de la liste des fuseaux bloqué : Chrome « Page crashed » 3 fois de suite après l'envoi du mp4 (machine chargée : ffmpeg de rendu en parallèle). Aucun code modifié.
