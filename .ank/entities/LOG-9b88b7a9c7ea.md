---
id: LOG-9b88b7a9c7ea
type: log
title: "Code fait: screen_title_style (citation|promesse) dans CONFIG_DEFAULTS captions, prompt+schema,"
created: 2026-10-12T11:34:47Z
author: w-0efd20071c1c
scope:
  - clipper/captions.py
  - tests/test_captions*.py
  - tests/fixtures/**
  - clipper/web/static/screens/channels.js
  - docs/**
  - config.example.toml
about: TASK-0efd20071c1c
seq: 4
schema: 4
version: 1
---

 erreur CaptionsError nommant la cle; tests FakeBackend ok (133 captions). Libelle channels.js NON ajoute: [captions] n'est pas dans _CHANNEL_FORM_SECTIONS (app.py, hors scope) et test_every_displayed_config_key_has_a_french_label refuserait un libelle orphelin. Controle reel: research/title-promise/RESULTAT.md (8 appels, script regenerate.py).
