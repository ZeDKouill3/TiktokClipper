---
id: TASK-a21f409cf69d
type: task
slug: retitrer-le-stock-aion-en-promesse-sans-toucher
title: Retitrer le stock AION en promesse sans toucher caption/hashtags
created: 2026-10-12T13:14:22Z
author: nicoc@zedk_ordi
status: done
scope:
  - tools/retitle_stock.py
  - tests/test_retitle_stock.py
blocked_by: []
done_criteria: |
  tools/retitle_stock.py patche seulement screen_title (tests verts), dry-run sur tout le stock + patch réel d'une vidéo, rapport research/retitle-stock/RAPPORT.md avec la commande de rendu par vidéo
criteria_by: creator
verify: [tests]
method: tdd
proof:
  - type: test
    ref: local/38506ea682d5@6c48daa
    tree: scope/1ce18f266e70
    criteria: 1d7d502cf22f
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---

## But
Appliquer le titre d'écran « promesse » (SPEC-08c2, `[captions] screen_title_style = "promesse"`, preset twitch-gaming) et le cadrage split centré (`[reframe] split_gameplay_align = "center"`) au stock de clips AION déjà rendus, SANS relancer les étapes longues (download, transcribe, audio, scenes, action, moments, vision, parts) et SANS toucher caption/hashtags.

## Pourquoi pas `clipper render --force-step captions`
L'étape captions réécrit aussi `caption` et `hashtags` du sidecar. Or `clipper/learning.py` rattache un post TikTok à son clip en comparant caption+hashtags (`_wanted_caption`, `_caption_matches`) : les clips déjà publiés ou programmés perdraient leurs statistiques. Seul le champ `screen_title` doit changer.

## À faire
1. Outil `tools/retitle_stock.py` (CLI) :
   - liste les vidéos du stock (workspace/, chaîne/preset twitch-gaming, jeu AION ; captions done) ;
   - pour chaque vidéo : génère les titres promesse avec la vraie fonction de captions (`clipper.llm`, config réelle résolue avec le preset de la vidéo) dans un dossier temporaire, comme pour research/title-promise/, puis recopie UNIQUEMENT `screen_title` dans la sortie captions réelle (et sidecars si concernés), clip par clip (même clip_id) ; caption, hashtags, title, tout le reste strictement identiques (vérifié dans le code par comparaison avant/après, échec bruyant sinon) ;
   - sauvegarde avant patch (copie horodatée sous research/retitle-backup/) ;
   - `--dry-run` : affiche par vidéo ancien → nouveau titre, n'écrit rien ;
   - `--video <id>` répétable pour limiter ;
   - aucun repli silencieux (ADR-ad2e) : un échec LLM ou un clip_id manquant = erreur nommée, la vidéo n'est pas patchée.
2. Après patch : relancer reframe, subtitles, render, qa de la vidéo (pas captions). Trouver la voie propre : file du worker (state/queue.json via l'API du serve http://127.0.0.1:8000, jeton local) avec force_step reframe si l'API le permet, sinon `clipper render --force-step reframe <id>` documenté. NE PAS lancer les rendus soi-même (4 h de calcul, l'orchestrateur les met en file) : l'outil affiche/prépare la commande ou l'appel API.
3. Tests unitaires `tests/test_retitle_stock.py` (FakeBackend, workspace factice) : seul screen_title change ; caption/hashtags identiques ; clip_id manquant = erreur ; dry-run n'écrit rien ; backup créé.

## Contrôle réel (petit)
`--dry-run` sur tout le stock, puis patch réel d'UNE vidéo AION ; rapport research/retitle-stock/RAPPORT.md : liste des vidéos, nombre de clips, anciens/nouveaux titres de la vidéo patchée, commande ou appel de rendu à lancer pour chaque vidéo.
