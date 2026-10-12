---
id: TASK-3f9d9187efbb
type: task
slug: split-twitch-jeu-centr-sur-l-image-enti-re-r-gla
title: "Split twitch : jeu centré sur l'image entière (réglage) et titre d'écran sur la ligne de séparation webcam/jeu"
created: 2026-10-12T10:21:18Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/reframe.py
  - clipper/render.py
  - tests/test_reframe*.py
  - tests/test_render*.py
  - tests/fixtures/**
  - docs/**
  - config.example.toml
  - clipper/web/static/screens/channels.js
blocked_by: []
done_criteria: |
  Demande utilisateur 12/10 sur les clips stream_split (preset local twitch-gaming, agencement split SPEC-b19b : webcam en haut 1040x640 à y=0, jeu en bas 1080x1280 à y=640) : (A) le jeu « n'est pas centré » : _split_gameplay_rect (reframe.py ~2843) centre la fenêtre du jeu dans la largeur restante une fois la colonne webcam exclue, donc le personnage et la barre de sorts sont décalés. Ajouter un réglage [reframe] split_gameplay_align dans CONFIG_DEFAULTS : "exclude_webcam" (défaut, comportement actuel inchangé pour les autres presets) | "center" (fenêtre centrée sur l'image source entière, sans note) ; valeur inconnue = erreur explicite nommant la clé. (B) titre d'écran sur la séparation : aujourd'hui le titre (render, encadré blanc arrondi, SPEC-6a86 : sobre, sans emoji ni superlatif) est placé pour le letterbox, au-dessus de la vidéo. Ajouter un réglage [render] (ex. title_split_center_y, entier, défaut 0 = placement actuel) qui, pour un clip stream_split, centre verticalement l'encadré du titre sur cette ordonnée de sortie (640 pour le preset) et horizontalement sur l'image, en restant dans le cadre 1080x1920 ; il ne doit pas chevaucher le sous-titre (split_subtitle_dest y=710..860) ni le badge s'il est activé : collision = erreur de config explicite au rendu (ADR-ad2e), pas de déplacement silencieux. Les clips letterbox/stream/blur du même preset gardent le placement actuel. Tests unitaires pour les deux réglages (défauts inchangés, center, ordonnée, collision). Vérifier SPEC-b19b et SPEC-6a86 (ank show) : si l'un des deux changements contredit une règle ratifiée, s'arrêter et faire released avec la règle citée. Rendu réel de contrôle (hors suite) : copier dans research/split-title-check/ un clip existant de workspace/v2896857793 (Chef_Zhab, AION, split) avec un preset copie de presets/twitch-gaming.toml (title_enabled = true, split_gameplay_align = center, title_split_center_y = 640), produire 1 mp4 et 3 images (début, milieu, fin) ; ne jamais écrire dans workspace/ ni output/ réels ; donner les chemins dans ank log. Documenter les deux réglages là où les autres réglages reframe/render sont documentés.
criteria_by: creator
verify: [tests]
method: tdd
proof:
  - type: test
    ref: local/336a3a86a9f3@9a59a1b
    tree: scope/94dbfe23c897
    criteria: c0deb613f0f0
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 4
---
