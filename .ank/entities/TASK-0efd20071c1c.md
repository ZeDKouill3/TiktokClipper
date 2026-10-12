---
id: TASK-0efd20071c1c
type: task
slug: titre-d-cran-promesse-tient-en-haleine-r-glage-s
title: "Titre d'écran « promesse » (tient en haleine) : réglage screen_title_style, spec successeur de SPEC-6a86"
created: 2026-10-12T11:27:36Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/captions.py
  - tests/test_captions*.py
  - tests/fixtures/**
  - clipper/web/static/screens/channels.js
  - docs/**
  - config.example.toml
blocked_by: []
done_criteria: |
  Demande utilisateur 12/10 : le titre d'écran (screen_title, encadré blanc, maintenant aussi sur la séparation webcam/jeu en twitch-gaming) doit tenir en haleine : annoncer ce que le spectateur attend dans la vidéo (l'enjeu, la chute à venir) sans la dévoiler, au lieu d'une citation (ex. actuel : « Je suis pas mort »). ÉTAPE 1, AVANT LE CODE : écrire avec ank new spec une spec successeur de SPEC-6a867ae54f94 (ank show SPEC-6a867ae54f94, à reprendre À L'IDENTIQUE pour tout le reste : format letterbox par défaut, .mp4 + .json sidecar, CTA désactivé par défaut, 6 mots au plus, aucun emoji par défaut, mots interdits, exemples), proposée (ne JAMAIS ank accept ni supersede toi-même si ank l'exige d'un humain : logge l'id et ce qu'il reste à faire), qui change la seule règle « Contenu » : nouveau réglage [captions] screen_title_style dans CONFIG_DEFAULTS de captions : "citation" (défaut, règle actuelle inchangée) | "promesse" : le titre annonce l'enjeu ou la chute du clip sans la révéler (question ou situation en suspens, ex. « Le craft T0 va-t-il passer ? », « Il tente le boss seul »), toujours sobre (mots interdits et emoji inchangés), et vrai : la promesse se réalise dans le clip, aucun fait, nom ou chiffre absent de la transcription ou de la justification du moment ; une série multipart garde le même titre (règle actuelle). Valeur inconnue = erreur explicite nommant la clé. Logger l'id de la spec puis ATTENDRE « spec ok » de l'orchestrateur dans le pane avant de coder. Code : prompt et description du champ screen_title dans le schéma JSON (le schéma prime sur le prompt) selon le style ; tests FakeBackend : défaut inchangé (prompt citation identique), promesse (consignes présentes dans prompt ET schéma), valeur inconnue ; libellé français du réglage dans channels.js (test_every_displayed_config_key_has_a_french_label) ; doc du réglage. Contrôle réel hors suite : un script sous research/title-promise/ qui régénère, en LECTURE SEULE de workspace/ (jamais d'écriture dans workspace/ ni output/), les screen_title en style promesse pour 8 clips AION existants (v2896857793, v2896810471, v2895875431) avec la config réelle, et écrit un tableau avant/après (titre actuel, nouveau titre, title du clip) dans research/title-promise/RESULTAT.md ; donner le chemin dans ank log. Aucun nom réel de chaîne dans le dépôt.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---
