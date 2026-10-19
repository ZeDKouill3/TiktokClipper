---
id: TASK-ef6e6c31363f
type: task
slug: pays-attendu-de-l-ip-par-compte-comptes-tiktok-u
title: Pays attendu de l'IP par compte (comptes TikTok UK)
created: 2026-10-19T07:36:58Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/network.py
  - clipper/browser.py
  - clipper/accounts.py
  - clipper/worker.py
  - clipper/web/**
  - tests/**
blocked_by: []
done_criteria: |
  Un compte peut porter un pays attendu (code ISO, ex. GB) modifiable dans l'écran Comptes ; le garde du navigateur piloté (browser._open_context et le contrôle du worker) compare l'IP au pays du compte s'il est renseigné, sinon à [network] expected_country ; un compte UK refuse d'ouvrir le navigateur sur IP FR et inversement, message nommant le compte et les deux pays ; comptes existants sans champ inchangés ; tests unitaires sans réseau.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---

Contexte : l'utilisateur veut publier des TikToks en anglais sur deux comptes UK (ClipManiaq3, clippyqaniaque), en pause depuis le 07/10 (0 vue sur ~30 posts). Aujourd'hui [network] expected_country vaut pour tous les comptes (FR) et browser._open_context appelle network.require_expected_country(config) sans savoir quel compte est piloté ; le worker fait aussi ce contrôle (worker.py ~ligne 1412).

À faire : champ optionnel « pays attendu » sur un compte (accounts.py, carnet local SPEC-6fa4/f348), exposé et modifiable dans l'API et l'écran Comptes (console v3, ADR-a308, erreurs en place SPEC-7715) ; require_expected_country prend le pays attendu en paramètre ; browser et worker passent celui du compte piloté. Le statut réseau affiché dans la console reste celui de [network]. Jamais de repli silencieux (ADR-ad2e) : pays inconnu = refus comme aujourd'hui.
