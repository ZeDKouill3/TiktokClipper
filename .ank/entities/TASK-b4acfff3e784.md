---
id: TASK-b4acfff3e784
type: task
slug: webcam-claude-r-pond-null-quand-aucun-rectangle
title: "Webcam : Claude répond null quand aucun rectangle ne montre le streamer (mini-carte prise pour la webcam)"
created: 2026-10-12T09:08:56Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/reframe.py
  - tests/test_reframe*.py
  - tests/fixtures/**
blocked_by: []
done_criteria: |
  Constat réel 12/10 (workspace/v2895815199/facecam.json et facecam/period_0.jpg, période unique) : aucun des 5 candidats n'était la webcam (la vraie, petite, en bas à droite près d'un logo partenaire, n'était pas candidate) ; Claude a répondu webcam=5 avec la raison « le rectangle 5 est proche de ce coin, mais il est décalé et ne montre que le décor... reste le meilleur choix » -> les 26 clips ont la mini-carte du jeu dans le panneau caméra. Attendu : (1) le prompt de _facecam_prompt (reframe.py vers la ligne 2106) dit explicitement que null est la bonne réponse quand aucun rectangle ne montre la personne du streamer elle-même, même si la webcam est visible ailleurs sur l'image, ou si un rectangle en est seulement proche, décalé, partiel ou ne montre que du décor ; jamais « le moins mauvais » choix ; (2) test qui vérifie ces consignes dans le prompt envoyé (FakeBackend) et test de régression qui rejoue la forme du cas (une période, réponse null) et vérifie que facecam est null et que les clips passent en repli (fallback) avec la raison journalisée ; (3) respecter SPEC-b19b (ank show SPEC-b19b) : schéma FACECAM_SCHEMA {webcam: entier|null, reason} inchangé, aucun garde-fou local contre la réponse de Claude sur une période unique ; si la correction exige de changer le schéma ou une règle de la spec, s'arrêter et faire released avec la raison. Aucun nom réel de chaîne dans le dépôt (fixtures ma_chaine) ; ne rien copier de workspace/ dans le dépôt. Ne pas refaire la vidéo v2895815199. Un test réel optionnel (un seul appel facecam sur la planche réelle) peut être lancé à la main pour contrôle, jamais dans la suite par défaut.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---
