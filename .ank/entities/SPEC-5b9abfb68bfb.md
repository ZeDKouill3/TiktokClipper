---
id: SPEC-5b9abfb68bfb
type: spec
slug: webcam-du-stream-trouv-e-par-p-riode-garde-fous
title: Webcam du stream trouvée par période, garde-fous locaux, recalage et contrôle QA (succède à SPEC-4a9b)
created: 2026-10-09T01:08:56Z
author: nicoc@zedk_ordi
status: superseded
scope:
  - clipper/reframe.py
  - clipper/render.py
  - clipper/subtitles.py
  - clipper/pipeline.py
  - clipper/llm/__init__.py
  - clipper/qa.py
  - clipper/web/static/**
references: [ADR-b16b71007578, ADR-fb9bcb1e98f5, ADR-ad2e562b1810, ADR-b1c17749b528, SPEC-6a867ae54f94]
supersedes: SPEC-4a9bf1f1b78b
ratified: 33688f0c849d
verified:
  - by: nicoc@zedk_ordi
    at: 2026-10-09T01:09:01Z
schema: 4
version: 3
---

## Objet
Succède à SPEC-4a9b (webcam par période, rectangles candidats numérotés choisis
par Claude), qui succédait elle-même à SPEC-76dc. Reprend à l'identique tout ce
qui est resté vrai (périodes, candidats, choix de Claude, agencement `top` /
`split`, badge, sous-titres, titre d'écran, zone sûre, JSON) et décrit le code
réel là où six tâches terminées (TASK-893d, 9957, 0cb1, 495c, a769, 5979) ont
changé les règles après la rédaction de SPEC-4a9b : vignettes zoom envoyées avec
la planche, garde-fous locaux sur la réponse de Claude, recalage du rectangle
choisi, visage exigé par clip quand le rectangle n'a pas de bords réels, contrôle
QA `empty_webcam` bloquant en `stream_split`, vocabulaire de config.

Constats réels du 2026-10-05 qui motivent la webcam par période (inchangés) :
sur une VOD (streamer à casque, petite webcam à gauche) le visage n'est reconnu
que sur 7 % des images clés, donc tous les clips tombaient en letterbox ; sur
une autre (Just Chatting plein écran au début, puis petite webcam en bas à droite
pendant le jeu) une seule position valait pour toute la VOD, douteuse. Une
disposition change au fil d'un stream : la webcam se retrouve donc par période,
jamais mémorisée d'un stream à l'autre (décision utilisateur 2026-10-05).

Chaque règle ci-dessous cite la fonction (`fichier:fonction`) et les réglages
(nom et défaut de `CONFIG_DEFAULTS`) qui la portent. Les réglages sans précision
de section sont ceux de `[reframe]`.

## Règles 1 à 3 : localisation, une fois par vidéo
`clipper/reframe.py:detect_facecam` ; résultat dans `facecam.json` (pas refait
sauf `--force` ; un `facecam.json` sans `periods` est une erreur explicite
« ancien format »), planches sous `facecam/period_<n>.jpg` et feuilles de
vignettes sous `facecam/period_<n>_zoom.jpg`. Tout le calcul d'images est local ;
un seul modèle lourd en VRAM à la fois, le détecteur de visages est fermé
(`detector.close()`, `gc.collect()`) avant le premier appel LLM (ADR-fb9b,
device via `clipper.gpu.get_device()`).

1. **Périodes (local).** `reframe.py:_sample_every`, `_find_periods`,
   `_has_big_face`. Environ une image clé par minute (`facecam_period_step`,
   défaut 60.0 s) sur toute la vidéo. Le Just Chatting est repéré par un visage
   d'au moins `facecam_fullscreen_face_height` (0.25) de la hauteur de l'image :
   au moins `facecam_period_min_samples` (2) images consécutives avec un grand
   visage, qui commencent dans les premières `facecam_period_lead_share` (0.1)
   des images, suivies d'au moins autant sans grand visage (le jeu), donnent DEUX
   périodes ; la limite est affinée à l'image clé près (dichotomie dans
   `detect_facecam` sur les images clés de `scenes.json`). Sinon (transition pas
   nette) UNE seule période, avec la raison écrite (`transition`) : la majorité
   des images décide. Un grand visage qui revient plus tard dans le jeu reste
   dans la 2e période. Les périodes sont jointives (la fin de l'une est le début
   de la suivante).

2. **Candidats (local).** `reframe.py:_period_candidates`. Sur
   `facecam_candidate_frames` (24) images équiréparties de la période
   (`_sample_keyframes`), (a) zones de visage à la même position (centre à moins
   de `max(facecam_tolerance` (40) px, `facecam_cluster_ratio` (1.0) × hauteur du
   visage`)`) sur au moins `facecam_candidate_min_frames` (2) images, calées sur
   le cadre réel de l'incrustation quand il est trouvé (`facecam_edge_gap_ratio`
   0.05, `facecam_edge_search_ratio` 3.0, `facecam_edge_min_gradient` 30.0,
   `facecam_edge_min_share` 0.8), sinon centrées sur le visage
   (`stream_face_height` 0.5, raison dans `edge_reason`) ; les coins de l'image
   sont aussi examinés agrandis (`facecam_corner_size` 0.3, `facecam_corner_zoom`
   2.0) ; (b) rectangles à cadre net (traits fixes sur au moins
   `facecam_candidate_edge_share` (0.9) des images, quatre côtés nets sur
   `facecam_candidate_side_share` (0.6) de leur longueur, côtés ajustés de
   `facecam_candidate_slack` (8) px, un côté à moins de `facecam_candidate_snap`
   (0.01) d'un bord d'image ramené sur ce bord et compté comme net) dont le
   contenu bouge (non figé, `facecam_frozen_min_diff` 8.0,
   `facecam_frozen_min_pixel_share` 0.001). Chaque candidat est mis au format du
   panneau caméra, couvre au moins `facecam_candidate_min_area` (0.005) de
   l'image et moins de `facecam_max_area` (0.25) ; les autres sont écartés avec
   leur raison (`rejected` dans `facecam.json`). Au plus `facecam_candidate_max`
   (8) candidats, numérotés de 1 dans l'ordre de lecture. Chaque candidat porte
   `support` (nombre d'images où il est vu) et `face_support` (nombre d'images où
   un visage y est détecté). Aucun candidat : pas d'appel, période sans webcam,
   raison écrite.
   Deux images sont ensuite fabriquées pour Claude :
   - la **planche** `period_<n>.jpg` (`reframe.py:_draw_board`) : `facecam_board_frames`
     (8) images de la période, chacune large de `facecam_board_tile_width` (480)
     px, rectangles dessinés, numéro dans une pastille collée au-dessus du
     rectangle, jamais dessus ;
   - la **feuille de vignettes** `period_<n>_zoom.jpg` (`reframe.py:_draw_zoom`,
     TASK-a769) : une ligne par candidat (numéro dans une bande à gauche, jamais
     sur le contenu), `facecam_zoom_frames` (3) recadrages du rectangle pris sur
     des images équiréparties de la période (parmi les 24), chacun de
     `facecam_zoom_tile_height` (240) px de haut.

3. **Claude (clipper.llm, ADR-b1c1).** `reframe.py:detect_facecam`,
   `_facecam_prompt`, `_facecam_check`. UN appel par période avec DEUX images
   fixes (`[planche, feuille de vignettes]`, jamais de vidéo), usage `facecam`
   (modèle rapide par défaut, `clipper/llm/__init__.py:CONFIG_DEFAULTS["usages"]`
   `"facecam": {"model": "fast"}`, réglable par `[llm.usages.facecam]`), réponse
   validée par `FACECAM_SCHEMA` `{"webcam": entier | null, "reason": texte}` : le
   numéro du rectangle qui est la webcam du streamer, ou null. Jamais de
   coordonnées demandées à Claude. Un numéro absent de la planche est une erreur
   explicite après la réparation prévue par clipper.llm (`repair_attempts` 1,
   ADR-ad2e) : `facecam.json` n'est pas écrit à moitié. Aucune mémoire d'un
   stream à l'autre.

   **Garde-fous locaux sur la réponse de Claude.** Ils sont journalisés
   (`log.warning`) et écrits dans `facecam.json`, période par période, sous
   `override` = `{from, to, reason}` (`null` s'il n'y en a pas). Le seuil commun
   est `facecam_face_stable_share` (0.8) × le plus grand `support` des candidats
   de la période.
   - (a) `reframe.py:_stable_face_instead` (TASK-495c) : si Claude choisit un
     candidat de genre cadre dont `face_support` vaut 0 (aucun visage : bannière
     de sponsor animée) et qu'un autre candidat a un visage (`face_support` > 0)
     ET un `support` au-dessus du seuil, c'est ce candidat visage qui est retenu
     (`override.from` = numéro de Claude, `override.to` = le candidat visage).
     Plusieurs candidats visage stables = `ReframeError` explicite (choix
     impossible sans deviner, ADR-ad2e).
   - (b) `reframe.py:_stable_face_over_null` et `_persistent` (TASK-a769,
     TASK-5979) : si Claude répond `null`, un candidat dont le `support` ET le
     `face_support` atteignent le seuil, et qui est **persistant** (son rectangle
     revient, IoU ≥ 0.5, dans au moins `facecam_face_stable_share` des périodes
     de la vidéo qui ont des candidats ; une seule période : persistant par
     construction), est retenu malgré la réponse (`override.from` = `null`).
     Les candidats stables mais NON persistants (visages de menus ou du jeu) ne
     contredisent pas Claude : sa réponse « aucune webcam » tient, un warning les
     nomme et la raison de la période les cite. Plusieurs candidats persistants =
     `ReframeError` explicite.

   **Recalage (local, après le choix, sans appel LLM).** `reframe.py:_refine_rect`
   et `_refine_side` (TASK-893d) : le rectangle retenu (celui de Claude ou de
   l'override) est recalé bord par bord sur la vraie incrustation, sur les
   images de la période. Chacun des 4 bords est cherché dans une bande de
   `facecam_refine_margin_ratio` (0.2) fois la largeur (bords gauche/droit) ou la
   hauteur (haut/bas) du rectangle, jamais plus loin ; sur chaque image, la
   position du plus fort gradient perpendiculaire au bord, moyenné sur le milieu
   du côté (`facecam_refine_band_trim` 0.2 retiré à chaque bout), vote si ce
   gradient dépasse `facecam_refine_min_gradient` (12.0) et
   `facecam_refine_peak_ratio` (4.0) fois la médiane de la fenêtre. Un bord est
   retenu quand au moins `facecam_refine_min_votes` (4) images votent à
   `facecam_refine_tolerance` (2) px près de la même position et qu'elles font au
   moins `facecam_refine_agreement` (0.5) des images qui votent ; sinon le côté
   reste tel quel et la raison est journalisée. Le visage stable n'est jamais
   traversé (son contour n'est pas un bord). Le rectangle recalé est agrandi au
   minimum au format du panneau caméra (`_size_camera_rect`) et ne coupe pas le
   visage ; un rectangle recalé de moins de 16 px, ou inutilisable, est
   abandonné (rectangle conservé tel quel, raison écrite). `facecam.json` garde
   `candidate_rect` (rectangle du candidat), `refined_rect` (rectangle recalé ou
   `null`), `refine_reason` (décalage retenu par côté, ou pourquoi rien n'a
   bougé) et `facecam` (le rectangle EFFECTIVEMENT utilisé : le recalé s'il y en
   a un).

   Mesuré sur les deux VOD de référence (vrai Claude, 2026-10-05, avant les
   vignettes) : 2 appels par VOD, environ 0,04 $ chacune, 7 à 11 s par appel ;
   la bonne webcam trouvée dans la période de jeu des deux, « aucune » pendant le
   Just Chatting.

## Règle 4 : choix par clip (une seule fois, tout ou rien)
`reframe.py:reframe` (branche `format = "letterbox"` et `layout = "stream_auto"`,
~l.3167) et `reframe.py:_clip_facecam`. Un clip prend la période qui contient
son début (la dernière dont `start` ≤ début du clip). Période sans webcam
(`facecam` nul) : letterbox, raison journalisée. Aucune image clé de
`scenes.json` dans le clip : letterbox, raison journalisée. Sinon le clip est
en stream si le rectangle y est présent et vivant sur au moins
`facecam_clip_min_share` (0.8) de ses images clés. Un rectangle est vivant sur
une image clé quand (a) son contenu n'est pas noir (luminosité moyenne ≥
`facecam_black_min_mean` 12.0 ou écart-type ≥ `facecam_black_min_std` 6.0) ;
(b) ses bords sont retrouvés au même endroit qu'à la localisation, sur au moins
`facecam_clip_edge_min_share` (0.5) d'une paire de côtés opposés, chaque côté à
`facecam_clip_edge_tolerance` (0.04) près ; (c) il n'est pas figé
(`reframe.py:_rect_is_frozen`, mêmes `facecam_frozen_*`, la première image clé
du clip n'est jamais figée).

**Visage exigé quand il n'y a pas de bords réels (nouveau, TASK-9957, TASK-0cb1).**
Quand la période porte un `edge_reason` non nul (rectangle centré sur le seul
visage, aucun bord d'incrustation retrouvé), il n'y a rien de comparable à
chercher par image clé : (b) ne s'applique pas, et seul un visage dans le
rectangle prouve que c'est bien la webcam. Le détecteur de visages est alors
OBLIGATOIRE pour contrôler le clip (construit par `reframe.py` avec le détecteur
de `[reframe] detector`, fermé après usage ; erreur explicite si absent). Sur
chaque image clé, le visage est cherché dans le recadrage du rectangle élargi de
`facecam_clip_face_margin` (0.25) de sa largeur/hauteur par côté, agrandi à
`facecam_clip_face_crop_height` (720) px de haut (à 1920x1080 un visage de 60-80
px échappe au détecteur courte portée sur l'image entière : 2/41 mesuré contre
39/41 sur le recadrage, v2894178473), avec une confiance d'au moins
`min_confidence` ; il compte si au moins `facecam_clip_face_min_inside` (0.9) de
sa boîte tombe dans le rectangle (une webcam déplacée garde un visage dont le
rectangle ne couvre que 0,64-0,73, la bonne en couvre 1,0). Le clip reste en
stream seulement si un visage est trouvé sur au moins
`facecam_clip_face_min_share` (0.5) des images clés ET que (a) et (c) tiennent
sur `facecam_clip_min_share` ; sinon letterbox, raison journalisée. Une image
clé illisible est une erreur explicite.

Jamais de bascule entre formats à l'intérieur d'un clip. La décision et sa
raison sont écrites par clip : `facecam_decision` = `{period, candidate,
reason}` dans `reframe/<clip_id>.json` (`candidate` est le numéro répondu par
Claude pour la période, `null` en letterbox ; en stream `reason` est la phrase de
Claude, en letterbox la raison du repli).

## Règle 5 : contrôle de la zone webcam du clip rendu (QA) — BLOQUANT en stream_split
**Décision de l'utilisateur, 2026-10-09.** Un panneau webcam vide est un clip
cassé. Le défaut `empty_webcam` (`clipper/qa.py:DEFECTS["empty_webcam"]`) est
demandé à l'IA du contrôle qualité UNIQUEMENT pour un clip dont le sidecar
porte `layout = "stream_split"` : le panneau webcam du haut ne montre ni visage
ni webcam sur la majorité des images (interface du jeu, bras de micro, décor,
écran de pause). Il est BLOQUANT (`clipper/qa.py:_BLOCKING_DEFECTS` =
`{"incomprehensible", "empty_webcam"}`, sévérité `BLOCKING`, invite de
`clipper/qa.py:_prompt`) : le clip est rejeté, et avec lui toute sa série
(`SERIES_PART_REJECTED`). Pour tout autre agencement (letterbox, stream `top`) ce
défaut n'est pas demandé (`clipper/qa.py:_NON_SPLIT_EXCLUDED_DEFECTS`,
`_excluded_defects`). Cela remplace la règle 5 de SPEC-4a9b (« aucun
contrôle ») et la piste d'un simple avertissement.

Inchangé : pendant un clip stream, aucun suivi (le rectangle source découpé ne
zoome ni ne se déplace) ; pas de webcam = letterbox, raison journalisée
(règle 4) ; un seul modèle de détection en VRAM à la fois, device via
`clipper.gpu` (ADR-fb9b).

## Choix de l'agencement stream (reprise)
Une fois qu'un clip est en stream (règles 1 à 4), deux axes de config bien
séparés : lequel des deux formats de sortie s'applique à un clip (letterbox ou
stream) et, une fois en stream, lequel des deux agencements le dessine
(`[reframe] stream_variant`).

- **Format** : `[reframe] format = "letterbox"` (défaut) ET `[reframe] layout`
  (`"letterbox"` défaut, ou `"stream_auto"`, `reframe.py:_LAYOUT_MODES`). La
  localisation et le choix par clip des règles 1 à 4 ne s'activent que si
  `format = "letterbox"` et `layout = "stream_auto"` ; toute autre valeur de
  `layout` est une erreur explicite au chargement (`reframe.py:_settings`) ;
  `layout = "stream_auto"` avec un `format` autre que `"letterbox"` aussi.
  (`format = "stream"` n'existe pas.)
- `stream_variant = "top"` (défaut) : agencement de SPEC-3a88, facecam agrandie
  en haut (`stream_camera_ratio` 0.4 de la hauteur de sortie à partir de
  `stream_top` 440), jeu en bas, titre d'écran au-dessus de la caméra,
  sous-titres dans la zone basse.
- `stream_variant = "split"` : agencement ci-dessous.

`stream_variant` n'est lu que pour un clip déjà en stream ; il ne change rien
au choix stream/letterbox lui-même. Une valeur inconnue est une erreur explicite
au chargement de la config (`reframe.py:_settings`, ADR-ad2e).

## Agencement split (reprise à l'identique)
Canevas inchangé 1080x1920 (SPEC-6a86). Deux zones fixes qui se partagent toute
la hauteur sans se chevaucher, plus un badge optionnel à leur jonction
(`reframe.py:_reframe_stream_split`, `_validate_split_geometry`) :

- **Webcam** (`split_webcam_dest`, défaut `{x: 20, y: 0, w: 1040, h: 640}`) : la
  webcam localisée (le rectangle de `facecam`, recalé s'il l'a été), agrandie en
  haut. Source recadrée (jamais étirée) au ratio de `split_webcam_dest`
  (1040:640) en la centrant sur le rectangle détecté — si le rectangle localisé
  n'a pas ce ratio, on rogne symétriquement l'excédent autour de son centre,
  sans jamais sortir du cadre source.
- **Jeu** (`split_gameplay_dest`, défaut `{x: 0, y: 640, w: 1080, h: 1280}`) : le
  reste de l'image, en bas, pleine largeur. Source recadrée (jamais étirée) au
  ratio de `split_gameplay_dest` (1080:1280), centrée horizontalement dans la
  largeur restante et excluant la zone source de la webcam quand c'est possible
  (`reframe.py:_game_window`) ; si l'exclusion complète ne tient pas dans le
  cadre source au ratio demandé, recadrage centré normal (cadrage moins
  favorable, noté dans le plan, ce n'est pas un échec).
- Les deux rectangles dest doivent tenir dans le canevas et ne jamais se
  chevaucher ; une config qui les fait se chevaucher, déborder ou tomber à ratio
  nul est une erreur explicite au chargement (ADR-ad2e).
- **Règle de ratio** : jamais de déformation, le rectangle source est toujours
  recadré au ratio du rectangle dest.

## Badge de chaîne (optionnel, reprise)
`[render] badge_enabled` (booléen, défaut `false`). Une fois actif, remplace
visuellement le pseudo texte de l'appel à l'abonnement (SPEC-6a86, `cta_handle`)
sur ce clip : si `badge_enabled` et `cta_enabled` sont tous deux actifs, le
pseudo texte ne s'affiche pas, mais la carte de fin (`cta_text`) n'est pas
affectée. `badge_enabled` ne dépend pas de `cta_enabled`.

- `badge_logo` (chemin d'un PNG, requis si `badge_enabled`) : logo dessiné sur
  fond noir dans un carré `badge_logo_size` (px, défaut `100`), glyphe réduit
  d'un facteur `badge_glyph_scale` (défaut `0.65`).
- `badge_name` (texte, requis si `badge_enabled`) : nom à droite du logo, même
  fond noir, taille `badge_font_size` (px d'em, défaut `40`).
- `badge_enabled` sans `badge_logo` ou sans `badge_name` est une erreur
  explicite (ADR-ad2e).
- `badge_dest` (`[reframe]`, défaut `{x: 330, y: 590, w: 420, h: 100}`) :
  position et taille du bandeau complet, à cheval sur la jonction webcam/jeu
  (la bande va de y=590 à y=690). Doit tenir dans la zone sûre TikTok : en
  dehors, erreur explicite (`reframe.py:_validate_split_geometry`).

## Style des sous-titres (agencement split, reprise)
Réglages `[subtitles]`, préfixe `split_`, indépendants du style karaoké
(`primary_color`/`secondary_color`/`emphasis_color`) : deux couleurs, le mot en
cours de prononciation et le reste, jamais de fond.

- `split_font_name` (défaut `"Poppins ExtraBold"`, police embarquée
  `clipper/assets/fonts`).
- `split_font_size` / `split_min_font_size` / `split_font_step` (px d'em,
  défauts `80` / `44` / `4`) : mêmes paliers de réduction que
  `letterbox_font_size` si le texte ne tient pas dans `split_subtitle_dest`.
- `split_uppercase` (défaut `true`).
- `split_text_color` (défaut `"white"`), `split_current_word_color` (défaut
  `"#9146FF"`, violet Twitch).
- `split_outline_color` (défaut `"black"`) et `split_outline` (px, défaut `10`).
- `split_shadow_enabled` (défaut `false`) ; si actif, `split_shadow_color` et
  `split_shadow_offset` (px, défaut `(2, 2)`).
- `split_subtitle_dest` (`[reframe]`, défaut `{x: 150, y: 710, w: 780, h: 150}`) :
  entièrement dans la zone jeu, sans recouvrir le badge, dans la zone sûre
  TikTok : sinon erreur explicite.
- Aucun fond derrière le texte. Si le texte ne tient pas à
  `split_min_font_size`, erreur explicite, jamais tronqué en silence.

## Titre d'écran et carte de fin (reprise)
`[render] title_enabled` (booléen, défaut `true`). Pour l'agencement split d'une
streameuse Twitch, `title_enabled = false`. La carte de fin reste gouvernée par
`cta_enabled` (défaut `false`). Le pseudo texte sous le titre (`cta_handle`) est
sans objet quand le badge le remplace et que le titre est désactivé.

## Zone sûre TikTok (reprise, SPEC-6a86)
Aucun texte au-dessus de y=160 ni au-dessous de y=1520, ni à gauche de x=150 ni
à droite de x=930 (`safe_top`, `safe_bottom`, `safe_left`, `safe_right`).
S'applique aux éléments de texte/overlay (badge, sous-titres), pas aux
rectangles vidéo (`split_webcam_dest`, `split_gameplay_dest`).

## Format stream actuel (défaut global, inchangé)
`stream_variant = "top"` reste le comportement par défaut : aucune config
existante n'est affectée. `split` est un choix explicite.

## JSON (sidecar)
Le champ `layout` d'un clip en agencement split vaut `"stream_split"` (distinct
de `"stream"` pour `top`), pour que la QA (`clipper/qa.py:_prompt`,
`_excluded_defects`, règle 5) et l'interface web (`clipper/web/static/**`)
distinguent les deux sans ambiguïté. `reframe/<clip_id>.json` porte en plus
`facecam_decision` (règle 4).

## Références
`research/madajel/agencement/madajel-tiktok.json` (valeurs de référence),
`research/madajel/agencement/rendu-5400.png` et `rendu-2400.png`,
`research/madajel/tiktok/*.png`. Fichiers locaux, hors dépôt (`.gitignore`) —
voir `research/BONNES-PRATIQUES.md`. Ne jamais citer le nom réel de la chaîne
dans ce dépôt public : « une streameuse Twitch ». Audit à l'origine de cette
spec : `research/reviews/drift-0910.md`.
