---
id: TASK-ee48b6241ea4
type: task
slug: l-gendes-jeu-de-la-cat-gorie-twitch-refus-du-mau
title: "Légendes : jeu de la catégorie Twitch, refus du mauvais jeu, plafond de longueur du titre d'écran, recaption du stock"
created: 2026-10-13T09:02:54Z
author: nicoc@zedk_ordi
status: open
scope:
  - clipper/captions.py
  - tests/test_captions.py
  - tools/recaption_stock.py
  - tests/test_recaption_stock.py
  - tools/retitle_stock.py
  - tests/test_retitle_stock.py
  - config.example.toml
  - clipper/assets/config.example.toml
  - docs/GUIDE.md
  - clipper/web/static/channels.js
blocked_by: []
done_criteria: |
  (1) le prompt de captions reçoit la catégorie Twitch du chapitre qui contient le clip (meta.json chapters) et la règle du jeu en tête l'utilise ; (2) une légende « Sur X, ... » dont X n'est ni la catégorie du chapitre ni dans le titre de la VOD est renvoyée au LLM (llm.SchemaError) ; (3) [captions] screen_title_max_chars (0 = pas de limite, défaut 0) refuse un titre d'écran plus long ; (4) tools/recaption_stock.py régénère seulement caption+hashtags des clips prêts jamais publiés dont la légende cite le mauvais jeu (dry-run, sauvegarde, rapport) ; (5) retitle_stock.py --max-chars N ne retitre que les titres plus longs que N ; tests verts
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---

Source : étude research/etude-vues-1013/RAPPORT.md (Q5, Q3) — dans le dépôt principal E:/ClaudeRandom/TiktokParseUpload (research/ est ignoré par git, lire en chemin absolu ; la liste des 36 clips au mauvais jeu est dans results_q5b.md, la méthode dans scripts/q5_mauvais_jeu.py).

Constat : 36 légendes citent un autre jeu que la catégorie Twitch de la VOD (« Sur Dofus Touch, Youby4 » sur une VOD AION ; « World of Warcraft vu par LaTaverneDePatatus » ; « Sur Satisfactory, Sardoche » ; « Sur GTA 6, MaatM » sur FC 27 ; ShojiFR après passage à Girls' Frontline 2 légendé « Sur Aion 2 »). Cause : _prompt de clipper/captions.py ne reçoit que le titre de la VOD, la chaîne, la justification, l'accroche et le transcript ; _keywords_rule (caption_keywords_first) demande le nom officiel du jeu en tête, lu « dans le titre, la transcription ou le contexte ». La catégorie Twitch (workspace/<id>/meta.json, champ chapters, déjà lu par download.py et moments.py) n'est jamais passée. Les VOD de Youby4 s'intitulent « Chill & Grind Templar 1900+ & Assassin 2100 » : le modèle devine le jeu.

À faire :
1. Prompt : passer la catégorie Twitch du chapitre qui contient le début du clip (et le titre de la VOD comme aujourd'hui). Règle : le jeu en tête = cette catégorie (nom officiel) ; si le streamer parle d'un autre jeu, ne jamais écrire « Sur <autre jeu> » mais par ex. « <Streamer> parle de <autre jeu> » ; catégorie hors jeu (Just Chatting...) : pas de « Sur <jeu> ». Sans chapitres (YouTube) : comportement actuel.
2. Validation mécanique (comme moderation_risk_terms / _validate_promesse_emoji) : une légende qui commence par « Sur X, » (ou « X vu par ») avec X ni égal (normalisé : casse, accents, chiffres romains/arabes, « 2 »/« II ») à la catégorie du chapitre, ni contenu dans le titre de la VOD, est refusée par llm.SchemaError (renvoyée au modèle). « Sur la chaîne de <streamer> » / « Sur le stream de » restent permis. Aucun repli silencieux (ADR-ad2e).
3. [captions] screen_title_max_chars (CONFIG_DEFAULTS, défaut 0 = pas de limite, entier >= 0 sinon erreur nommée) : consigne dans le prompt + validation (SchemaError si plus long). Documenter dans config.example.toml (les deux copies) et docs/GUIDE.md. Étude : titres > 35 caractères médiane 80 vues contre 251 (corrélation, pas causalité).
4. tools/recaption_stock.py (modèle : tools/retitle_stock.py, même structure, mêmes garde-fous) : pour les clips prêts SANS tiktok_post ni youtube_post, détecter avec la même fonction de validation ceux dont la légende cite le mauvais jeu ; régénérer UNIQUEMENT caption + hashtags (vraie fonction captions dans un dossier temporaire, comme retitle_stock), recopier caption+hashtags dans captions.json ET dans le sidecar output/<vid>/<clip>.json (le plan et la publication lisent le sidecar ; vérifier où caption est lu), sauvegarde horodatée, --dry-run, --video, rapport markdown. Ne jamais toucher un clip publié : clipper/learning.py rattache un post à son clip par caption+hashtags.
5. tools/retitle_stock.py : option --max-chars N qui ne retitre que les clips dont screen_title dépasse N (garde le style du clip : pipeline.json title_style / sidecar screen_title_style), en appliquant screen_title_max_chars=N à la génération ; jamais un clip publié.
Tests : FakeBackend (jamais le vrai Claude), aucun réseau. Ne pas lancer les outils sur le vrai workspace : l'orchestrateur le fera.
