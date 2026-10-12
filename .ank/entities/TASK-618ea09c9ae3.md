---
id: TASK-618ea09c9ae3
type: task
slug: veille-contr-le-webcam-sur-les-miniatures-des-vo
title: "Veille : contrôle webcam sur les miniatures des VOD avant le choix de Claude (spec successeur de R6 de SPEC-bdd9)"
created: 2026-10-12T09:24:26Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/veille.py
  - clipper/web/static/**
  - tests/test_veille*.py
  - tests/test_web_veille*.py
  - tests/fixtures/**
blocked_by: []
done_criteria: |
  Demande utilisateur 12/10 : écarter dès la veille les VOD sans webcam (une VOD à la mini-carte prise pour la webcam a donné 26 clips faux). ÉTAPE 1, AVANT LE CODE : écrire une spec avec ank new spec (proposée, ne JAMAIS ank accept : l'orchestrateur la ratifie) qui complète R6 de SPEC-bdd9 (ank show SPEC-bdd9 ; R6 dit aujourd'hui « un appel texte seul ») ; logger son id dans ank log puis attendre ma réponse « spec ok » dans le pane avant de coder. Contenu attendu de la spec et du code : (1) après la collecte et avant le choix de Claude, les N meilleurs candidats (réglage webcam_check_max dans CONFIG_DEFAULTS de veille, défaut 30, 0 = contrôle désactivé ; ordre = celui déjà utilisé pour présenter les candidats à Claude) ont leur miniature officielle (thumbnail_url Helix/YouTube, déjà dans le candidat ; jamais de scraping, téléchargement borné en temps et parallèle borné, aucune image stockée au-delà du relevé du jour sous state/veille/) assemblée en UNE planche numérotée (numéro hors de l'image, lisible) ; (2) UN appel llm.ask usage veille_webcam (image fixe, ADR-b1c1) avec schéma {items: [{n, webcam: oui|non|doute, reason}]} et check (numéros existants, tous présents, uniques) ; consigne : webcam = la caméra filmant le streamer incrustée sur le jeu ; une mini-carte, un widget, un personnage du jeu ne sont pas une webcam ; dans le doute répondre doute ; (3) non -> candidat retiré du choix, ajouté à excluded avec la raison « pas de webcam sur la miniature : <reason> » (visible à l'écran Veille comme les autres exclusions) ; doute, miniature absente/illisible (placeholder 404_processing de Twitch) -> gardé, marqué webcam: doute|inconnue dans le candidat ; (4) échec de l'appel ou du téléchargement -> aucun repli silencieux (ADR-ad2e) : llm webcam status error dans le fichier du jour et le journal, les candidats restent tous dans le choix avec webcam inconnue (décision explicite écrite dans la spec : un échec du contrôle ne bloque jamais la veille, il se voit) ; (5) l'écran Veille montre la pastille webcam (oui/doute/inconnue) sur chaque proposition. Tests : FakeBackend, miniatures fausses (aucun réseau), planche construite, exclusions, doute, échec, réglage 0. Respecter l'échéance globale du relevé (un contrôle coupé par l'échéance = status deadline, candidats gardés). Aucun nom réel de chaîne dans le dépôt.
criteria_by: creator
verify: [tests]
method: tdd
proof:
  - type: test
    ref: local/09711e7a336e@40aa6e7
    tree: scope/2ec3647bc0cf
    criteria: 3bce3a15a106
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---
