# Guide utilisateur

Ce guide suppose l'installation faite (voir `README.md`). Toutes les
commandes sont `python -m clipper <commande> ...` ; `--config chemin.toml`
(défaut `config.toml`) et `-v`/`-vv` (journal détaillé des étapes, voir
*Sortie détaillée* ci-dessous) marchent sur toutes.

## Les 13 étapes du pipeline

Une vidéo passe, dans l'ordre, par `download, transcribe, audio, scenes,
action, moments, vision, parts, captions, reframe, subtitles, render, qa`.
Chaque étape lit ses entrées sous `workspace/<video_id>/` et y écrit son
résultat ; une étape dont le résultat existe déjà n'est pas relancée (sauf
`--force`).

1. **download** — télécharge la vidéo (yt-dlp), au mieux en 1080p mp4.
2. **transcribe** — transcrit l'audio mot par mot (faster-whisper), avec
   correction de la transcription et du vocabulaire de noms propres par LLM.
3. **audio** — repère les pics sonores (rires, réactions, cris...) ; tourne
   avant `scenes`, qui s'en sert si l'étape `action` est activée.
4. **scenes** — détecte les changements de plan (scenedetect) et extrait des
   images clés ; avec `[action] enabled = true`, décode aussi les fenêtres
   autour des pics sonores hors parole (`scenes.json` porte alors
   `"peak_windows": true`).
5. **action** — désactivée par défaut (`[action] enabled`). Repère sans LLM
   les passages où il se passe quelque chose (pics audio, densité de plans),
   puis fait décrire leurs images par le LLM : `action.json`.
6. **moments** — le LLM choisit les moments forts du transcript, notés selon
   `rubric.toml` (SPEC-53f3) ; avec `[moments] candidates =
   "transcript+action"`, les passages d'`action.json` deviennent aussi des
   candidats (source « action »).
7. **vision** — regarde les images clés de chaque moment candidat et ajuste
   la note (bonus visuel), sans relancer le LLM de `moments`.
8. **parts** — découpe un moment trop long en plusieurs parties (chacune une
   vidéo séparée) si besoin.
9. **captions** — génère titre, légende, hashtags et accroche par clip.
10. **reframe** — calcule le plan de recadrage vertical (letterbox ou suivi de
    visage selon la config, voir *Formats* ci-dessous).
11. **subtitles** — génère les sous-titres (.ass), mot par mot, avec emphase
    choisie par LLM.
12. **render** — assemble tout avec ffmpeg (image, sous-titres, titre
    d'écran, audio normalisé) : `output/<video_id>/<clip_id>.mp4`.
13. **qa** — contrôle qualité automatique (résolution, durée, silence de
    tête, image noire...) ; un clip n'est prêt que si `qa.is_ready` le dit.

Un seul modèle lourd est en VRAM à la fois (ADR-fb9b) : les étapes tournent
en séquence et chacune libère le sien (whisper dans `transcribe`, détecteur
de visages dans `reframe`) avant la suivante.

## Sortie détaillée (`-v` / `-vv`)

Sans `-v`, la sortie ne change pas : les lignes `[étape] démarrée` / `[étape]
terminée en X s` de la CLI (indépendantes du niveau de journal). Avec `-v`
(niveau `INFO`), la console montre en plus, au fil de l'eau :

- début et fin de chaque étape, avec sa durée ;
- la progression des étapes longues, au plus toutes les 30 s ou tous les
  10 % : `download` (pourcentage et débit), `transcribe` (minutes d'audio
  traitées sur le total, facteur temps réel), `reframe`/`render`/`qa` (clip
  i/N avec sa durée), `vision` (lot d'images i/N) ;
- une ligne par appel LLM (usage, modèle, tokens entrée/sortie/cache, coût,
  durée, et son issue : réussi, réessai avant réparation, ou échec) ;
- en sélection par jury (`moments`, ADR-ff87), la décision de chaque candidat
  jugé (score final, retenu, rejeté, veto, ou exploration) ;
- un résumé de `moments` (candidats notés, retenus, raison des rejets) ;
- à la fin d'un run qui va jusqu'au bout : un résumé (durée par étape, nombre
  de clips, statuts qa, coût LLM total et par usage, chemin de `output/`).

`-vv` (niveau `DEBUG`) ajoute le détail : chaque événement de progression
(chaque appel LLM, chaque segment transcrit, chaque clip ou lot traité) sans
attendre le seuil des 30 s / 10 %, plus quelques lignes internes (détail des
appels LLM). `scenes` n'est pas concerné ici (sa propre progression est une
tâche séparée).

## Modes : review et auto

Réglé par `mode` à la racine de `config.toml` (`"review"` ou `"auto"`,
ADR-ad2e).

**review** (défaut) — `run` s'arrête après `moments`/`parts`, statut
`awaiting_review`. Chaque moment proposé attend une décision :

```powershell
python -m clipper decide <video_id> <moment_id> accepted
python -m clipper decide <video_id> <moment_id> rejected
python -m clipper decide <video_id> <moment_id> adjusted --start 12.5 --end 45.0 [--comment "..."]
```

...ou via l'interface web (`python -m clipper serve`, `http://127.0.0.1:8000`) :
liste des vidéos, moments à trancher, lancement du rendu, aperçu des clips.
Une fois chaque moment décidé, `python -m clipper render <video_id>` reprend
jusqu'au bout (moments refusés retirés, bornes ajustées reportées).

**auto** — `run` va jusqu'au bout tout seul ; l'étape `qa` remplace la revue
humaine. Une erreur transitoire (quota Claude, réseau, surcharge) remet la
vidéo en file d'attente (statut `queued`, `retry_at` calculé depuis
`[pipeline] retry_delays`) au lieu d'abandonner ou de produire un résultat
dégradé en silence :

```powershell
python -m clipper queue              # reprend les videos dont retry_at est passe
python -m clipper queue --watch --interval 60   # tourne en boucle
```

Au-delà de `[pipeline] max_attempts` échecs transitoires consécutifs (défaut
5), ou pour toute autre erreur : la vidéo passe `failed`.

`python -m clipper status <video_id>` affiche l'état courant en JSON à tout
moment (utile en mode auto pour suivre une vidéo sans l'interface web).

## Formats de sortie (`[reframe]`)

Deux réglages indépendants dans `config.toml` :

- `format` — `"letterbox"` (défaut, zoom fixe, sans suivi de visage) ou
  `"crop"` (suivi de visage, **option figée** : conservée telle quelle, pas
  d'évolution prévue).
- `layout` — seulement avec `format = "letterbox"` : `"letterbox"` (défaut)
  ou `"stream_auto"` (SPEC-3a88). En `stream_auto`, la facecam de la vidéo
  est détectée une fois (si elle a un visage à position quasi fixe sur une
  bonne part des images clés, dans une zone limitée de l'écran) ; les clips
  où elle est présente rendent en format stream (facecam fixe agrandie en
  haut, jeu en bas, **jamais de bascule dans un clip**), les autres en
  letterbox classique.
  Une fois un clip en stream, `stream_variant` (SPEC-76dc) choisit
  l'agencement visuel : `"top"` (défaut, comportement inchangé, décrit
  ci-dessus) ou `"split"` (webcam en haut sur ~1/3 de la hauteur, jeu en bas
  pleine largeur, badge de style optionnel à la jonction) — voir *Agencement
  stream split* ci-dessous.

Le contrat de sortie d'un clip (`SPEC-6a47`, succède à `SPEC-6127`) :
`.mp4` vertical 1080x1920 + `.json` sidecar (titre, légende, hashtags,
rapport qa...). En letterbox, titre d'écran en haut, sous-titres dans la
bande floue du bas. Voir *Appel à l'abonnement* ci-dessous pour le pseudo
d'affichage et la carte de fin optionnels.

```toml
[reframe]
format = "letterbox"
layout = "stream_auto"   # ou "letterbox"
```

## Appel à l'abonnement (SPEC-6a47, `preset` par style)

Désactivé par défaut : sans configuration explicite, le rendu, le sidecar et
la légende restent identiques à `SPEC-6127`. Utile pour un contenu tiers
(ex. un·e streameur·se dont on republie les meilleurs moments) : pseudo
d'affichage discret sous le titre d'écran pendant tout le clip, carte de fin
« Abonne-toi ! » sur les dernières secondes, ligne d'appel et hashtags
supplémentaires dans la description. S'applique en letterbox et en stream
(`layout = "stream_auto"`) ; ignoré en `format = "crop"` (option figée,
`cta` reste `false` dans le sidecar, ce n'est pas une erreur).

Un preset par style est un fichier de config séparé, passé avec `--config` :

```powershell
python -m clipper run https://www.twitch.tv/videos/<id> --config presets/ma-chaine.toml
```

`presets/ma-chaine.toml` :

```toml
[render]
cta_enabled = true
cta_handle = "twitch.tv/ma_chaine"
cta_seconds = 2.0                        # duree de la carte de fin (defaut)
cta_text = "Abonne-toi !"                # texte de la carte de fin (defaut)

[captions]
cta_line = "Abonne-toi sur Twitch pour plus de lives !"
cta_hashtags = ["#twitch", "#horreur"]
```

`cta_enabled` sans `cta_handle` (côté `[render]`) ou sans `cta_text`, ou un
`cta_seconds` <= 0 ou >= la durée d'un clip, est une erreur explicite
(ADR-ad2e : jamais de CTA à moitié activé) — jamais une carte de fin ou un
pseudo tronqué ou absent en silence. Les réglages de mise en page (tailles de
police, marges) vivent dans `CONFIG_DEFAULTS` de `clipper/render.py`
(`cta_handle_font_size`, `cta_card_font_size`...).

## Agencement stream split (SPEC-76dc, `preset` par style)

Un second agencement visuel pour les clips déjà en stream (`stream_variant`,
voir *Formats de sortie* ci-dessus), pensé pour reproduire le montage
« webcam en haut, jeu en bas » qu'une streameuse ou un streamer fait
déjà lui-même : deux zones fixes qui se partagent toute la hauteur (jamais de
déformation, chaque zone est recadrée au ratio de son rectangle de
destination), un badge de style optionnel (logo + pseudo, fond noir par
défaut ou sans fond) à leur jonction, et un style de sous-titres à deux
couleurs (mot en train
d'être prononcé dans une couleur distincte, pas de fond). `title_enabled`
(nouveau réglage, défaut `true` — comportement inchangé) permet de retirer le
titre d'écran, utile ici puisque ce modèle n'en a pas.

`presets/ma-chaine-stream.toml` :

```toml
[reframe]
layout = "stream_auto"
stream_variant = "split"

[render]
title_enabled = false             # pas de titre d'ecran pour ce modele
cta_enabled = false                # ni carte de fin
badge_enabled = true
badge_logo = "presets/logo-ma-chaine.png"   # PNG, jamais dans clipper/assets
badge_name = "ma_chaine"

[subtitles]
split_current_word_color = "#9146FF"   # violet Twitch (defaut) ; #RRGGBB ou un nom
split_shadow_enabled = false
```

Les zones (`split_webcam_dest`, `split_gameplay_dest`, `badge_dest`,
`split_subtitle_dest`, toutes `[reframe]`) ont des valeurs par défaut qui
correspondent à la maquette de référence ; réglables (mêmes clés
`{x, y, w, h}` en pixels du canevas 1080x1920), mais une config qui les fait
déborder du canevas, se chevaucher entre elles ou sortir de la zone sûre
TikTok (badge, sous-titres) est une erreur explicite au chargement
(ADR-ad2e). `badge_enabled` sans `badge_logo` (fichier
introuvable inclus) ou sans `badge_name`, ou activé sur un layout qui n'a pas
de zone badge (letterbox, stream `"top"`, format crop), est aussi une erreur
explicite : le badge remplace le pseudo d'affichage de l'appel à l'abonnement
quand les deux sont actifs, sans toucher à la carte de fin.

Deux réglages ajustent le cadrage et le titre du split (TASK-3f9d9187efbb) :

- `[reframe] split_gameplay_align` — `"exclude_webcam"` (défaut, inchangé) centre
  la fenêtre du jeu dans la largeur qui reste une fois la colonne de la webcam
  exclue ; `"center"` la centre sur l'image source entière (personnage et barre
  de sorts au milieu), sans note, même si elle recouvre la colonne de la webcam.
  Toute autre valeur est une erreur explicite nommant la clé.
- `[render] title_split_center_y` — entier, défaut `0` (placement actuel du
  titre). Avec une valeur > 0 (ex. `640`, la ligne de séparation webcam/jeu), le
  titre d'écran des clips `stream_split` est centré verticalement sur cette
  ordonnée de sortie et horizontalement sur l'image, dans le cadre 1080x1920.
  S'il chevauche les sous-titres (`split_subtitle_dest`) ou le badge actif
  (`badge_enabled`), ou sort du cadre, le rendu s'arrête avec une erreur
  explicite (jamais de déplacement silencieux, ADR-ad2e). Sans effet sur les
  clips letterbox, stream ou blur.

Le groupe logo + nom est toujours centré horizontalement sur `badge_dest`
(largeur ajustée au contenu, mesurée avec la vraie police) : jamais de vide
asymétrique. `badge_background` (défaut `"black"`) dessine un rectangle
plein derrière tout le bandeau ; `"none"` retire ce rectangle derrière le
nom — le nom reste lisible grâce à `badge_name_outline` /
`badge_name_outline_color` (contour, défauts `3` / `"black"`) et, si besoin,
une ombre réglable (`badge_name_shadow_enabled`, défaut `false`, puis
`badge_name_shadow_color` et `badge_name_shadow_offset`). Le carré du logo
lui-même n'est jamais noir : il est toujours rempli avec `badge_logo_fill`
(quel que soit `badge_background`), par défaut vide (`""`) pour
échantillonner automatiquement la couleur au coin de l'image du logo
(ex. le fond violet déjà présent dans un logo Twitch) — réglable à une
couleur fixe si besoin.

## Console de gestion

`python -m clipper serve` lance la console web (FastAPI + page statique, sans
étape de build) et, en sous-processus, le **worker** qui traite la file. Par
défaut : `http://127.0.0.1:8000` (`[web] port` ou `--port`). La page ne fait
aucun traitement vidéo, audio ou LLM : elle lit `workspace/`, `output/`,
`state/` et `presets/`, et demande le travail au worker. Fermer l'onglet, ou
même arrêter le serveur, n'interrompt pas une vidéo en cours de traitement.
`python -m clipper worker` lance le worker seul (sans interface).

### Les 8 écrans

La navigation (barre latérale, onglets en bas sur téléphone) donne :

1. **Accueil** (tableau de bord) : vidéos en cours (étape, progression, durée
   restante estimée quand le pipeline la donne), file d'attente (passer une
   vidéo en tête, la retirer), vidéos en échec ou remises en file avec leur
   raison, VOD « à confirmer » issues de la surveillance, clips à valider,
   prochaines publications, coût LLM du jour et de la semaine par usage, état
   matériel (GPU ou CPU).
2. **Vidéos** : liste filtrable (style, statut, texte) ; ajout par URL avec
   choix du style (ou « sans style » pour `config.toml` seul) ; fiche
   avec la frise des 12 étapes, le journal suivi en direct, « relancer depuis
   cette étape » et « annuler ».
3. **Revue** (mode `review`) : lecteur de la source calé sur le moment, score,
   accroche, justification du jury, bornes début/fin ajustables ; raccourcis
   `A` (accepter), `R` (refuser), `J`/`K` (suivant/précédent), espace
   (lecture). « Lancer le rendu » n'est actif que quand chaque moment a une
   décision ; sinon la raison s'affiche.
4. **Clips** : galerie 9:16 par vidéo et par style, lecteur, fiche du clip
   (titre d'écran, description, hashtags, partie N/M, `qa_status`, `issues`),
   édition de la description et des hashtags, du titre d'écran (re-rendu),
   approuver / refuser, re-rendre, télécharger le mp4, copier la description.
5. **Styles** : liste (nom, source, surveillance, mode),
   création et édition d'un preset par formulaire, chaque champ montrant sa
   valeur héritée de `config.toml` tant que le preset ne la redéfinit pas ; une
   erreur de validation s'affiche sous le champ. Deux outils : l'**éditeur
   d'agencement** (canevas 1080×1920 : zones webcam, jeu, badge, sous-titres à
   glisser et redimensionner sur une image clé d'une vidéo du style,
   enregistrées dans `[reframe]` ; un agencement qui déborde ou se chevauche
   est refusé avec le message de `reframe`) et l'**aperçu du style des
   sous-titres** sur une phrase d'exemple, rendu par le pipeline.
6. **Publication** : par compte, clips `approved` / `scheduled`, calendrier
   hebdomadaire des créneaux (glisser-déposer sur un créneau libre),
   télécharger, copier la description, « marquer publié », « repasser en
   attente ». La mise en ligne reste manuelle : le dépôt ne publie rien sur
   TikTok.
7. **Statistiques** : par clip (résultats importés, décisions humaines, QA),
   coûts LLM par vidéo, usage et période, durée par étape, import CSV des
   statistiques de la plateforme. Une section **Apprentissage** (voir plus bas)
   montre ce que la boucle a appris des résultats réels.
8. **Réglages** : `config.toml` en formulaire (mode global, dossiers, backend
   et modèle LLM par usage, surveillance), écriture validée avant d'être
   enregistrée ; section « Accès » (hôte, jeton masqué) en lecture seule avec
   la commande à lancer.

En plus de ces écrans, **Veille** (juste après Accueil ; à la place de Vidéos dans
la barre du bas sur téléphone) propose les VOD du jour à clipper, voir
« Veille des sujets chauds » plus bas. L'écran Clips a un filtre « Archivés ».

Toute erreur de l'API s'affiche en clair (toast et à la place de l'objet),
jamais un tiret muet. Les actions qui ont un inverse (décision de revue,
approbation) proposent « Annuler » pendant 5 secondes ; les autres (annuler un
traitement, refuser une série, supprimer un style) demandent confirmation.

### Pause et reprise d'un compte (écran Comptes)

La case « Prêt à publier » d'un compte se clique (SPEC-f348). Cochée, un clic
met le compte **en pause** : la case se décoche et l'écran affiche « En pause
(manuel) depuis le <date> » (heure de Paris). Décochée parce que le compte est
en pause, un clic le fait **reprendre** : la connexion est revérifiée puis la
pause levée ; si le compte n'est toujours pas prêt, le message dit pourquoi.
Décochée sans pause (jamais connecté, session expirée), le clic lance « Se
connecter » comme avant.

Effet d'une pause sur la file de publication :

- les entrées de la file restent en attente avec la raison (« en pause ») ;
  rien ne part sur un autre compte ;
- les publications déjà programmées côté plateforme ne sont pas touchées ;
- les clips et l'écran Publication ne proposent plus ce compte ; une
  publication existante qui le vise l'affiche « (en pause) » ;
- les statistiques du compte restent relevables.

À la reprise, une publication immédiate dont le créneau est déjà passé part à
la boucle suivante du worker.

### La file de traitement

Ajouter une vidéo (écran Vidéos, ou VOD confirmée) l'inscrit dans
`state/queue.json`. Le worker traite **une vidéo à la fois**, chacune dans un
processus enfant `python -m clipper` ; « annuler » termine ce processus.
L'ordre est modifiable (passer en tête, retirer). Une erreur transitoire
remet la vidéo en file avec sa raison et l'heure de reprise (`retry_at`) ;
une erreur définitive la passe en `failed`. Le worker lit les mêmes fichiers
`state/` que le serveur, sous verrou de fichier.

### Presets de style en surcouche

Un style est un fichier `presets/<nom>.toml` : une **surcouche** fusionnée
clé par clé sur `config.toml`. Seules les clés redéfinies figurent dans le
fichier ; tout le reste est hérité. La table `[channel]` (validée par
`CONFIG_DEFAULTS` de `clipper/channel.py`) décrit le style lui-même :
`display_name`, `source_url`, `watch`, `watch_interval_s`,
`watch_min_duration_s`, `mode`, `timezone`, `logo`. Le compte de publication
et les créneaux réguliers n'y sont plus : ils appartiennent au compte (écran
Comptes), chaque publication choisit le sien. Un ancien preset qui porte
encore `slots` / `tiktok_account` est migré au démarrage (créneaux repris sur
ce compte s'il n'en a pas déjà, clés retirées du fichier). Exemple `presets/ma_chaine.toml` :

```toml
[channel]
display_name = "ma_chaine"
source_url = "https://www.twitch.tv/ma_chaine/videos"
watch = true
mode = "review"

[reframe]
format = "stream_auto"
stream_variant = "split"
```

Le nom (`ma_chaine`, minuscules, chiffres, `_` et `-`) est celui du fichier.
Le formulaire de l'écran Styles lit et écrit ce même fichier ; le même
preset s'utilise en ligne de commande avec
`--config presets/ma_chaine.toml`.

### Le dossier `state/`

Tout état hors vidéo vit en fichiers JSON sous `state/`, jamais en base ni en
mémoire seule (un redémarrage reprend où l'on en était) :

- `state/queue.json` : la file de traitement ;
- `state/watch/<chaine>.json` : surveillance (vidéos vues, VOD en attente de
  confirmation, dernière erreur) ;
- `state/publish/<chaine>.json` : file de publication et créneaux pris ;
- `state/veille/` : la veille des sujets chauds (voir plus bas).
- `state/learning/` : la boucle d'apprentissage (`links.json`, `sync.json`, `coach.json`).

Chemins réglables dans `[worker]`, `[watch]`, `[publish]` et `[veille]`. Les vidéos
elles-mêmes restent sous `workspace/<video_id>/` et `output/<video_id>/`.

### Apprentissage : la section de l'écran Statistiques

Sous les statistiques TikTok, la section **Apprentissage** (ADR-c260) lit
`GET /api/learning` ; elle ne lance aucun calcul ni appel à Claude, le worker
seul verse, recalibre et coache. Trois blocs :

- **État de la boucle** : date du dernier versement, dernière erreur (en
  rouge, avec l'étape en cause), clips reliés à leur post TikTok et non
  reliés (avec la raison : aucun post ne correspond, plusieurs posts
  correspondent), clips exclus de l'apprentissage avec leur raison (trop
  récent, compte sous le minimum de posts à vues, absent du relevé, service
  sans statistiques) ;
- **Poids par juge** : poids, accord avec les résultats réels, nombre de cas
  et raison quand le poids reste à 1 (`state/jury_weights.json`) ;
- **Coach des prompts** : quand `coach_min_new_cases` clips mûrs nouveaux
  existent (10 par défaut) et que `coach_min_interval_days` jours se sont
  écoulés depuis le dernier passage (7 par défaut, réglages de `[learning]`),
  le worker demande à Claude une retouche de perspective par juge (sauf
  `conformite`, jamais coaché), la rejoue sur des cas passés et consigne le
  résultat dans `state/learning/coach.json`. Chaque proposition affiche la
  métrique avant → après (erreur moyenne de prédiction, plus bas est mieux),
  la perspective en place et la perspective proposée.

**Le coach ne s'applique jamais seul.** Tu décides : **Adopter** écrit la
perspective proposée dans `[jury.judges.<juge>] perspective` de `config.toml`
(comme l'écran Réglages : les commentaires du fichier sont perdus, un
avertissement le signale), **Refuser** la met de côté ; dans les deux cas la
proposition est marquée avec la date et `web`, et ne peut plus être décidée
une seconde fois. Rien n'est modifié dans `clipper/jury.py` ni dans les poids.

### Test A/B des titres par compte (`[repartition] title_ab_*`)

Chaque compte TikTok listé dans `title_ab_accounts` a UN style de titre d'écran par jour
(`citation`, `promesse`, `promesse_emoji`, SPEC-810f), et le style tourne chaque semaine pour que
chaque compte essaie chaque style. Le titre est incrusté dans la vidéo : un clip ne peut donc partir
que sur un compte dont le style du jour est le sien.

- Réglages : `title_ab_enabled` (faux par défaut), `title_ab_start` (jour 1 de la semaine 1, heure de
  Paris), `title_ab_accounts` et `title_ab_styles` (listes ordonnées). Style du compte de rang `i` la
  semaine `w` (0 pour la première, 7 jours chacune) : `title_ab_styles[(i + w) % n]`. Une valeur
  invalide (style inconnu, compte en double, date illisible) est une erreur nommée.
- Le rendu écrit dans le `.json` du clip `screen_title_style` et `title_shown` (titre réellement
  dessiné). Un clip sans l'un des deux n'est éligible à aucun compte du test, jamais deviné.
- Le plan du soir ne place sur un compte du test que les clips de son style ; chaque refus (style
  différent, style inconnu) est listé dans le plan (`title_ab.refused`) et l'écran Publication affiche
  le style du compte et celui de chaque clip. Les comptes hors liste ne changent pas.
- Une vidéo nouvelle reçoit, à l'étape captions, le style qui a le moins de clips disponibles ; il est
  écrit dans son `pipeline.json` (`title_style`) et gardé à la relance.
- `python tools/retitle_stock.py --video <id> --style promesse_emoji` retitre une vidéo du stock dans
  un style donné (défaut `promesse`) ; relancer ensuite le rendu depuis `reframe`.

### Surveillance des VOD

Avec `watch = true` dans `[channel]`, le worker interroge la `source_url` du
style toutes les `watch_interval_s` secondes (rien n'est téléchargé pour
lister). Il ignore les VOD plus courtes que `watch_min_duration_s`, les
directs en cours et celles déjà vues. En mode `auto` les nouvelles VOD sont
mises en file ; en mode `review` elles apparaissent « à confirmer » sur
l'Accueil, où l'on choisit de les confirmer (mise en file) ou de les ignorer.
Une erreur de listage est affichée (`last_error`), le style reste surveillé.

### Veille des sujets chauds

L'écran **Veille** propose chaque jour 2-3 VOD à clipper, choisies par Claude
d'après ce qui monte sur Twitch, YouTube et Steam et d'après tes goûts. Elle
est **désactivée par défaut** et ne fait rien tant que tu ne l'actives pas.

**Activer.** Réglages › Veille : coche `enabled`, écris tes goûts en texte
libre (`taste`), règle l'heure du relevé (`run_at`, heure de Paris, 07:00 par
défaut) et saisis les clés ci-dessous. Le premier relevé a lieu à l'heure
réglée, ou tout de suite avec « Rafraîchir ». Le relevé et le choix de Claude
tournent dans le worker (`python -m clipper worker` doit être lancé) ; l'écran
web ne fait qu'afficher et déposer tes demandes.

**Clés Twitch** (obligatoires pour Twitch) : sur
<https://dev.twitch.tv/console>, « Register Your Application » (nom libre,
URL de redirection `http://localhost`, catégorie « Application Integration »),
puis « Manage » : copie le *Client ID* et génère un *Client Secret*. Saisis-les
dans `twitch_client_id` et `twitch_client_secret`.

**Clé YouTube** (obligatoire pour YouTube) : dans la console Google Cloud
(<https://console.cloud.google.com>), crée un projet, active **YouTube Data
API v3**, puis « Identifiants » › « Créer des identifiants » › « Clé API ».
Saisis-la dans `youtube_api_key`. Le quota par défaut est de 10 000 unités par
jour ; un relevé coûte 1 unité (un appel `videos.list`), donc très loin de la
limite.

**Steam** n'a besoin d'aucune clé : le relevé interroge les API publiques de Steam
(`ISteamChartsService` pour le top des joueurs et le **pic du jour**,
`ISteamUserStats` pour les joueurs **à l'instant** d'un jeu précis, et la page
publique `steamcommunity.com/games/<appid>/memberslistxml` pour les **abonnés**
du groupe Steam d'un jeu). Les appels par jeu sont plafonnés et espacés (voir
les réglages ci-dessous) ; une source qui échoue est signalée en rouge dans le
bandeau des sources (« Steam (joueurs hors top) », « Steam (abonnés) »).

**IGDB (sorties de jeux)** utilise les clés Twitch déjà saisies (IGDB appartient à
Twitch, même jeton d'application) : rien de plus à créer. L'écran affiche une
section **Sorties de jeux** entre les propositions et les meilleurs clips, sous
la forme d'un **calendrier** :

- **En-tête** « Calendrier du <date> » avec les puces « N récentes », « N à venir »
  et « Source : IGDB ».
- **Bandeau « Sorties récentes »** : une carte par jeu, dans l'ordre de la collecte
  (les plus attendus d'abord), avec la jaquette, la pastille « Aujourd'hui » ou
  « Sortie J+N », « Tendance » si le jeu est dans le relevé du jour, le nom, trois
  plateformes au plus puis « +n », « Portage » (un jeu déjà sorti ailleurs qui arrive
  sur une nouvelle plateforme), le nombre d'hypes IGDB et, quand il y en a, la ligne
  de tendance (ventes Steam FR, viewers Twitch FR, joueurs Steam, abonnés Steam) et
  la puce « Communauté » ou « Peu de monde ».
- **Frise « À venir (N j) »** : une colonne par jour d'aujourd'hui à J+`upcoming_days`
  (la colonne d'aujourd'hui est mise en avant et montre les sorties du jour), la
  première jaquette en grand avec ses hypes, cinq au plus puis « +n autres »,
  « Aucune sortie notable » pour un jour vide.
- **Téléphone** (écran de 760 px ou moins) : la frise laisse la place à une liste par
  jour, qui ne montre que les jours avec sortie et « N jours sans sortie notable »
  entre deux.
- **Détail** : un clic (ou un toucher) sur une jaquette ouvre un panneau avec la
  jaquette, le nom, la date en toutes lettres, J+N / J-N / Aujourd'hui, toutes les
  plateformes, les hypes, la tendance (avec la courbe des joueurs quand la veille en
  a une), « Portage sur nouvelle plateforme » et le lien « Voir sur IGDB » (nouvel
  onglet). Il se ferme par la croix, la touche Échap ou un clic à côté.

Les **jaquettes sont chargées par ton navigateur** depuis `images.igdb.com`
(adresse directe, `t_cover_big`) : Clipper ne les télécharge, ne les relaie et ne
les stocke jamais. Sans jaquette (ou si l'image ne charge pas), la vignette porte le
nom du jeu. Si tu n'as pas accès à `images.igdb.com`, le calendrier reste complet,
avec des vignettes à nom.

« +n autres » sous une liste veut dire que la collecte a coupé au plafond
(`igdb_recent_max`, `igdb_upcoming_max`) ; « n sorties écartées (moins de N hypes) »
compte les jeux sous `igdb_min_hypes` qui n'étaient pas en tendance. Une fenêtre
sans sortie s'affiche « Aucune sortie dans la fenêtre » ; si IGDB répond en erreur,
la cause est affichée en rouge et aucun calendrier n'est montré. Les propositions et
les lignes de « Ce qui monte » dont le jeu vient de sortir portent un badge
« Sortie J+N », et Claude donne la priorité aux jeux dans leur fenêtre de sortie.

**Filtre de communauté.** Une VOD n'est proposée que si son jeu a une communauté
suffisante : il suffit qu'**un seul** des quatre seuils soit atteint (ils sont reliés
par « ou ») : `community_min_steam_players` joueurs Steam (1000), `community_min_steam_followers`
abonnés Steam (10 000), `community_min_twitch_viewers` viewers Twitch FR (200) ou
`community_min_hypes` hypes IGDB (50). Un chiffre inconnu n'atteint aucun seuil : un
jeu inconnu de toutes les sources est écarté (compté dans « n VOD écartées :
communauté insuffisante ou jeu inconnu »). Les joueurs Steam comptent le **pic du jour**
quand le jeu est dans le top Steam, sinon le chiffre **à l'instant du relevé** (nommé
comme tel partout : « Steam (pic du jour / à l'instant) » dans « Ce qui monte »,
« à l'instant du relevé » dans le calendrier) ; les abonnés viennent de la page
`memberslistxml` du jeu, les viewers de Twitch FR, les hypes d'IGDB. La colonne
« Communauté » de « Ce qui monte » dit « ok » (avec le critère atteint) ou
« insuffisante ».

**Courbe et gain d'abonnés.** Les courbes de joueurs Steam (une par jeu dans « Ce qui
monte », et dans le détail d'une sortie en tendance) et le gain d'abonnés « +G (7 j) »
sont construits depuis l'**historique des relevés de la veille** : ils n'existent pas
tant que la veille n'a pas tourné plusieurs jours. Le premier jour, la courbe dit « 1 jour
de mesure » et le gain « historique insuffisant » ; aucune valeur n'est inventée. Les
points « pic du jour » et « à l'instant » ont deux couleurs.

**Diversité.** `max_vods_per_game` (1) limite le nombre de VOD proposées par jeu :
Claude doit varier les jeux, et un choix qui dépasse le plafond est refusé.

**Les treize réglages** de Réglages › Veille liés au calendrier, à la communauté et à
la diversité : `upcoming_days` (14, horizon des sorties à venir, >= 1),
`release_window_days` (15, jours après la sortie où un jeu est « récent », >= 0),
`igdb_min_hypes` (5, hypes IGDB minimum, >= 1), `igdb_recent_max` (12) et
`igdb_upcoming_max` (20) (plafonds d'affichage, >= 1), `steam_players_lookups_max` (30)
et `steam_followers_lookups_max` (200) (jeux interrogés par relevé, >= 0),
`steam_followers_pause_s` (1.0 s entre deux appels, >= 0,2), `community_min_steam_players`,
`community_min_steam_followers`, `community_min_twitch_viewers` et `community_min_hypes`
(seuils ci-dessus, >= 0) et `max_vods_per_game` (>= 1). Une valeur hors bornes est refusée
à l'enregistrement avec le message de la veille. L'ancien réglage `igdb_releases_max`
est **ignoré** s'il traîne dans `config.toml` : il est remplacé par `igdb_recent_max` et
`igdb_upcoming_max`.

**Historique de 30 jours (courbes).** Dès le premier relevé, chaque jeu **suivi** (communauté
suffisante et au moins une VOD candidate, au plus `trend_games_max` jeux par relevé) reçoit
une courbe de `trend_days` jours (30), sans attendre que la veille ait tourné un mois. La
colonne « 30 j » de « Ce qui monte » et la carte de chaque proposition la dessinent
(une ligne par série, une couleur par série, légende, « n j mesurés / 30 », « pic <date> »,
et pour la carte le résumé « s4 → s1 », la moyenne par jour de la plus ancienne à la plus
récente des quatre dernières semaines). Un jeu non suivi dit « jeu non suivi ». D'où vient
chaque courbe :

- **Avis Steam par jour** (« Steam (avis 30 j) ») : l'histogramme des avis de la page
  du jeu, `store.steampowered.com/appreviewhistogram/<appid>`. C'est un **endpoint non
  documenté** de Steam, utilisé seul, plafonné et espacé (`steam_reviews_pause_s`). S'il
  change de forme, la source « Steam (avis 30 j) » passe en erreur avec l'URL et le détail,
  les courbes d'avis disent « indisponible », et **tout le reste continue** (les autres
  courbes, le test d'accès, le choix de Claude).
- **VOD FR Twitch par jour** (« Twitch (VOD 30 j) ») : les VOD (`archive`) en français encore
  en ligne, publiées chaque jour, lues par Helix *Get Videos* sur un mois. Twitch ne rend
  que **500 vidéos** par jeu : pour un très gros jeu les jours anciens sont **inconnus**
  (la légende l'écrit : « plafond Twitch 500 VOD »), jamais comblés.
- **Viewers Twitch FR** : relevés propres de la veille elle-même (`history/<date>.json`),
  un point par jour où la veille a tourné.

Un jour sans mesure n'est **jamais** un zéro ni une valeur lissée : la courbe a un trou (les
deux jours voisins ne sont reliés que s'ils se suivent), et une série à un seul point dit
« 1 jour de mesure ». **Ce que Claude reçoit** : pour chaque jeu suivi, une ligne par série
avec les quatre moyennes hebdomadaires, le pic, le dernier jour, « dernier vs pic » et
« s1 vs s2 » ; il conclut lui-même (un pic de sortie retombé vaut moins qu'un jeu qui
monte ou qui tient). L'écran ne recalcule rien : il dessine les points et le résumé écrits
par la veille.

**Test d'accès des VOD Twitch, par jeu.** Une VOD n'est proposée que si son accès a été
vérifié (yt-dlp, sans rien télécharger). Le test se fait jeu par jeu, des VOD les plus vues
aux moins vues, et **s'arrête dès que `max_vods_per_game` VOD accessibles sont trouvées**
pour ce jeu. Chaque VOD a jusqu'à `twitch_access_attempts` essais (5), séparés de
`twitch_access_retry_pause_s` (3 s) : une VOD réservée aux abonnés est écartée tout de
suite (« n VOD écartées : réservées aux abonnés »), une VOD qui échoue à **tous** ses essais
(coupure réseau, connexion fermée) est écartée (« n VOD écartées : injoignables après N
essais »), et les VOD d'un jeu déjà servi ne sont pas testées (« n VOD non testées : jeu
déjà servi (M VOD accessibles par jeu) »). Plus aucune VOD n'est proposée « non vérifiée » :
la mention « Accès non vérifié » n'existe plus, et un jeu dont toutes les VOD sont écartées
garde sa courbe. Le réglage `twitch_access_check_max` est **retiré** : s'il traîne dans
`config.toml`, il est ignoré.

**Un relevé qui ne bloque plus le worker.** Le relevé quotidien (et « Rafraîchir ») tourne
dans un fil d'arrière-plan du worker : publications, statistiques, surveillance et vidéos
continuent pendant ce temps, et un seul relevé tourne à la fois (une demande « Rafraîchir »
reçue pendant un relevé est prise au tour suivant). Une erreur dans ce fil est journalisée,
jamais avalée. Un relevé interrompu par l'arrêt du worker est repris au démarrage suivant.

**Voies parallèles et échéance.** Les sources sont interrogées en parallèle, **une voie par
hôte** (Twitch/IGDB, YouTube, API Steam, store Steam, steamcommunity, usher), en trois
phases séparées par une barrière : 1) Twitch puis IGDB, YouTube, Steam puis ventes FR ;
2) joueurs et abonnés Steam, puis le filtre de communauté et les jeux suivis ; 3) test
d'accès, avis Steam et VOD Twitch sur un mois ; puis Claude choisit. Le budget visé est un
relevé complet (sources, test d'accès, choix de Claude) en **moins de 10 minutes**
(environ 5 min mesurées, 6,5 min au pire nominal ; le poste le plus lourd, les abonnés
Steam, vient de la pause de 3 s imposée par Steam : baisse `steam_followers_lookups_max`
avant de toucher à la pause). `veille_deadline_s` (480 s) est l'**échéance globale** des
trois phases : quand elle tombe, tout ce qui n'est pas fini **s'arrête**, la source est
marquée « incomplète » en orange avec son message (« échéance de N s atteinte : k … non
relevé(s) »), une source qui n'avait pas commencé est « échéance atteinte avant le début »,
les VOD non encore testées sont écartées (« n VOD non testées (échéance) »), le bandeau
**« Relevé incomplet : échéance de N s atteinte à <heure> »** apparaît, et Claude choisit
avec ce qui est relevé, une donnée manquante restant « inconnue ».

**Les onze réglages** de cet historique, dans Réglages › Veille : `trend_days` (30, jours de
courbe, 7 à 90, et `history_days` doit être au moins égal), `trend_games_max` (40, jeux suivis
au plus par relevé, 0 = aucune source de tendance appelée), `veille_deadline_s` (480, 60 à
3600), `twitch_access_attempts` (5, 1 à 10), `twitch_access_retry_pause_s` (3,0 s, 0 à 60),
`steam_reviews_pause_s` (2,0 s, 0,2 à 60), `twitch_history_pages_max` (5, 1 à 5) — ces sept
sont dans le formulaire — et, sous « autres réglages », `steam_reviews_retry_max` (3),
`steam_reviews_retry_wait_max_s` (60), `twitch_history_retry_max` (2) et
`twitch_history_retry_wait_max_s` (60) pour les réessais sur HTTP 429. Une valeur hors bornes
est refusée à l'enregistrement avec le message de la veille.

Les clés s'écrivent dans `config.toml` (`[veille]`), jamais ailleurs : l'écran
Réglages ne les réaffiche jamais (il dit seulement « saisie » ou « absente »),
un champ laissé vide garde la valeur actuelle, et elles sont masquées dans le
journal. Une source dont la clé manque ou qui répond en erreur est signalée en
rouge sur l'écran avec la cause ; les autres sources et le choix de Claude
continuent avec ce qu'il y a.

**Coût.** Les appels aux sources sont gratuits (voir le quota YouTube
ci-dessus). Claude est appelé **une seule fois par relevé** (usage `veille`,
texte seul, modèle `strong`) ; ce coût apparaît avec les autres dans le coût
LLM du tableau de bord. Aucun candidat : Claude n'est pas appelé.

**Utiliser.** Chaque proposition affiche la raison de Claude et les chiffres
(Δ sur 7 jours ; « pas assez d'historique » tant que moins de 2 relevés
existent, « hors Steam » pour un jeu sans correspondance). Choisis le style,
puis « Clipper » (la VOD part dans la file et ne sera plus proposée) ou
« Ignorer » (elle ne sera plus jamais proposée). Parmi les clips des VOD de
veille terminées le même jour, les `best_clips_per_day` meilleurs sont gardés ;
les autres sont **archivés** : masqués de l'écran Clips (filtre « Archivés »
pour les revoir), restaurables par « Restaurer », jamais supprimés. Un clip
déjà approuvé, programmé ou publié n'est jamais archivé.

**Où sont les fichiers.** Tout est en JSON lisible sous `state/veille/`
(`[veille] state_dir`) : `days/<date>.json` (état du jour : sources, jeux,
candidats, propositions), `history/<date>.json` (chiffres relevés, gardés
`history_days` jours, base de la moyenne 7 jours), `selection/<date>.json`
(clips gardés, archivés, restaurés), `seen.json` (VOD déjà mises en file ou
ignorées), `refresh.json` (demande de « Rafraîchir », consommée par le worker)
et `twitch_token.json` (jeton d'application Twitch, sans le secret).

### Notifications

Un toast s'affiche à chaque passage d'une vidéo à `done`, `failed`,
`awaiting_review` ou `queued`. Le bouton cloche de l'en-tête active en plus
les **notifications du navigateur** : la permission n'est demandée qu'à ce
clic, jamais au chargement de la page, et le réglage reste local au
navigateur (`localStorage`). Si le flux temps réel (`/api/events`) tombe, un
bandeau « connexion perdue » apparaît et la page interroge l'API toutes les
5 secondes.

### Accès distant par jeton

Par défaut la console n'écoute que sur `127.0.0.1` et n'exige rien. Pour la
joindre depuis un téléphone du même réseau :

```toml
[web]
token = "un-jeton-long-et-aleatoire"
```

```bash
python -m clipper serve --host 0.0.0.0
```

`--host` (ou `[web] host`) autre que `127.0.0.1` **exige** `[web] token` :
sans jeton, `serve` refuse de démarrer avec un message qui nomme la clé, sans
lancer ni serveur ni worker. Avec un jeton, toute requête `/api` et `/media`
sans jeton valide reçoit 401 ; la page, elle, se charge et affiche une saisie
du jeton, gardé ensuite dans un cookie du navigateur (`SameSite=Strict`).
Un script peut l'envoyer dans l'en-tête `x-clipper-token`. Le jeton ne se
modifie pas depuis l'interface : fichier `config.toml`, puis redémarrage.

Limites, à lire avant d'ouvrir le port :

- **Réseau local seulement.** Un seul jeton partagé, un seul utilisateur, pas
  de comptes ni de limitation de tentatives.
- **Pas de TLS** : le jeton et les vidéos circulent en clair. Sur un réseau de
  confiance seulement.
- **N'expose jamais le port sur Internet** sans reverse proxy TLS devant
  (Caddy, nginx...) ; sans lui, ne redirige pas le port depuis la box.

## Configuration (`config.toml`)

Chaque étape (module `clipper/<etape>.py`) déclare son propre
`CONFIG_DEFAULTS` : une table `[<etape>]` dans `config.toml` est validée
contre ce dict — clé absente de `CONFIG_DEFAULTS` refusée, section sans
module `clipper.<etape>` ou sans `CONFIG_DEFAULTS` refusée aussi (voir
`clipper/config.py`). La liste ci-dessous couvre les réglages les plus
utiles ; pour le détail complet et à jour, `CONFIG_DEFAULTS` dans le module
concerné fait foi.

Racine (pas de section) :

```toml
mode = "review"          # ou "auto"
workspace_dir = "workspace"
output_dir = "output"
```

`[pipeline]` — `max_attempts` = 5 (échecs transitoires avant `failed`),
`retry_delays` = `[300, 900, 1800, 3600]` (secondes, le dernier sert
au-delà), `feedback_examples` = 10 (décisions passées données en exemple au
LLM de `moments`).

`[llm]` — voir *Modèles et consommation Claude* ci-dessous.

`[download]` — `cookies_file`, `cookies_from_browser` (vidéos privées/âge
restreint), `js_runtimes` = `"node"`.

`[transcribe]` — `model` = `"small"` (taille faster-whisper : tiny, base,
small, medium, large-v3...), `language` (défaut détection auto),
`beam_size` = 5, `vad_filter` = `true`, `vocab`/`transcript_fix` = `true`
(corrections par LLM), `fix_chunk_words` = 3000, `fix_parallel` = 4.

`[scenes]` — `threshold` = 27.0 (sensibilité scenedetect), `jpeg_quality` =
95, `extract_parallel` = 4.

`[action]` — voir *Candidats d'action* dans le README : `enabled` = false
(défaut), `window_seconds` = 30, `step_seconds` = 15, `audio_weight` et
`cuts_weight` = 1.0, `audio_peaks_full` = 3, `audio_peak_min_db` = 6.0,
`cuts_ratio_full` = 3.0, `min_score` = 0.6, `max_passage_seconds` = 90,
`max_passages_per_hour` = 12, `frames_per_passage` = 4,
`max_images_per_hour` = 48, `batch_size` = 8, `max_width` = 768,
`parallel` = 4.

`[audio]` — `sample_rate` = 16000, `window_seconds` = 1.0,
`median_window_seconds` = 15.0, `peak_threshold_db` = 6.0.

`[moments]` — `selection` = `"single"` (ou `"jury"`, forcé en mode auto),
`rubric_path` = `"rubric.toml"` (chemin utilisé tel quel ; `"builtin"` :
grille embarquée dans le paquet, sans fichier local), `candidates` = `"transcript"` (ou `"transcript+action"`, voir *action*),
`action_snap_seconds` = 3, `max_transcript_chars`,
`chunk_chars` (découpe les longues vidéos), `exploration_share` = 0.1 (part
de candidats hors grille stricte, pour ne pas se figer sur les mêmes
formats), `short_clips` = false (interrupteur « clips courts », voir
ci-dessous), `short_min` = 20 et `short_max` = 45 (bornes en secondes).

**Clips courts.** Un clip de 90 s accroche mal (premier post TikTok mesuré :
3,9 % de visionnage complet). Avec `short_clips = true`, les durées de la
grille (`[durations]`, clip unique **et** parties de série) sont remplacées par
`short_min`–`short_max`, et la consigne donnée à Claude exige que le clip
démarre directement sur le moment fort (accroche dans les 2 premières
secondes, pas de mise en place) tout en restant compréhensible seul. À `false`
(défaut), rien ne change. Le réglage se pose par style
(`presets/<style>.toml`, `[moments] short_clips = true`) ou dans `config.toml`.
Par vidéo, la case « Clips courts » du formulaire *Ajouter une vidéo* a trois
états : non précisé (valeur du style), coché (oui), décoché (non, même si le
style dit oui) ; en ligne de commande : `clipper run <url> --short-clips` ou
`--no-short-clips`. Le mode utilisé est écrit dans `moments.json`
(`short_clips`, `short_min`, `short_max`) et affiché sur la fiche de la vidéo ;
`workspace/<id>/rubric-short.toml` est la copie de la grille aux bornes courtes
que lit l'étape parts. Une valeur invalide (`short_clips` non booléen,
`short_max` ≤ `short_min`) est une erreur explicite.

`[vision]` — `window_seconds` = 10, `batch_size` = 8, `max_width` = 768,
`parallel` = 4.

`[parts]` — `part_overlap_seconds` = 3, `parallel` = 4. La grille des durées
est celle de l'étape moments (`rubric.path` de `moments.json`) ; l'ancienne clé
`rubric_path` de `[parts]` est refusée.

`[captions]` — `title_max_chars` = 100, `caption_max_chars` = 300,
`hashtags_max` = 8, `hook_words_max` = 8, `screen_title_words_max` = 6,
`screen_title_allow_emoji` = `false` (SPEC-6a86 : sans configuration
explicite, un `screen_title` avec un emoji est refusé — activer l'option ne
le rend pas obligatoire, elle permet seulement d'en accepter un au plus),
`screen_title_style` = `"citation"` (SPEC-08c2 : `"citation"` = phrase prononcée dans le clip ou fait précis ; `"promesse"` = le titre annonce l'enjeu ou la chute du clip sans la révéler, ex. « Le craft T0 va-t-il passer ? », toujours sobre et vrai ; `"promesse_emoji"` = même promesse, mais le titre se termine toujours par « ... », un espace et UN seul emoji d'émotion (🤔 et « ?... » refusés, emoji autorisé sans `screen_title_allow_emoji`, une réponse mal formée est renvoyée au modèle puis échoue, jamais corrigée en silence) ; une autre valeur est une erreur explicite),
`screen_title_forbidden_words` (liste de mots d'emphase clickbait refusés
dans `screen_title`, insensible à la casse et aux accents, mot entier ;
réglable par preset de style), `parallel` = 4 (moments traités en
parallèle), `cta_line`/`cta_hashtags` (SPEC-6a47, vides par défaut, voir
*Appel à l'abonnement* ci-dessus).

`[reframe]` — voir *Formats* ci-dessus, plus le détecteur de visages
(`detector` = `"mediapipe"`, `min_confidence` = 0.5, `sample_fps` = 5.0),
`output_width`/`output_height` = 1080/1920, `letterbox_zoom` = 1.3.
`stream_variant` (SPEC-76dc, voir *Agencement stream split* ci-dessus) et ses
zones (`split_webcam_dest`, `split_gameplay_dest`, `badge_dest`,
`split_subtitle_dest`).

`[subtitles]` — `font_name` = `"Poppins ExtraBold"`, `font_size` = 96,
`min_words_per_group`/`max_words_per_group` = 2/4, `emphasis` = `true`
(emphase choisie par LLM), `parallel` = 4 (clips traités en parallèle, lu par
le pipeline). Rien ne s'affiche pendant un silence (TASK-9ee7) : `hold_s`
(0,3 s) — un groupe de mots reste affiché au plus ce temps après la fin de
son dernier mot, borné par le début du suivant — `gap_s` (0,6 s) — un écart
de plus que ça avant le mot suivant coupe le groupe, rien n'est affiché
pendant l'écart — `max_word_s` (1,5 s) — la fin d'un mot isolé, parfois
étirée par faster-whisper sur le silence qui suit, est bornée à ce temps
depuis son début. Style de l'agencement stream split (SPEC-76dc, préfixe
`split_`, jamais d'appel LLM) : `split_font_name`/`split_font_size`,
`split_uppercase`, `split_text_color`/`split_current_word_color` (le mot en
train d'être prononcé) — couleurs `#RRGGBB` ou un nom (`white`, `black`,
`purple`...), pas le format ASS des réglages ci-dessus —
`split_outline_color`/`split_outline`, `split_shadow_enabled` (défaut
`false`) et `split_shadow_color`/`split_shadow_offset`.

`[render]` — `crf` = 20, `x264_preset` = `"medium"`, `nvenc_preset` = `"p5"`
(si GPU), `audio_bitrate` = `"192k"`, normalisation loudness
(`loudnorm_i/tp/lra`), réglages du titre d'écran (`title_font_size`,
`title_pad_x/y`...) et de l'accroche (`hook_seconds`, `hook_font_size`,
`hook_margin_top`). Encodeur choisi par `clipper.gpu` (`h264_nvenc` si CUDA
détecté, sinon `libx264`). `title_enabled` (SPEC-76dc, défaut `true` —
comportement inchangé) : désactive le titre d'écran, sur tout layout.
`cta_enabled`/`cta_handle`/`cta_seconds`/`cta_text` (SPEC-6a47, désactivé par
défaut, voir *Appel à l'abonnement* ci-dessus) et leurs réglages de mise en
page (`cta_handle_font_size`, `cta_card_font_size`...). Badge de style
(SPEC-76dc, voir *Agencement stream split* ci-dessus) : `badge_enabled`
(défaut `false`), `badge_logo`, `badge_name`, `badge_logo_size` = 100,
`badge_glyph_scale` = 0.65, `badge_logo_fill` = `""` (échantillonné au coin
du logo si vide), `badge_font_size` = 40, `badge_background` =
`"black"` (`"none"` = pas de rectangle derrière le nom), `badge_name_outline`
= 3, `badge_name_outline_color` = `"black"`, `badge_name_shadow_enabled` =
`false`, `badge_name_shadow_color` = `"black"`, `badge_name_shadow_offset` =
`[2, 2]`.

`[qa]` — `expected_width`/`expected_height` = 1080/1920,
`duration_tolerance` = 0.5, seuils de silence et d'image noire
(`black_min_seconds` = 1.0 avertit, `black_block_seconds` = 3.0 bloque le
clip), `parallel` = 4.

`[feedback]` — `journal_path` = `"state/feedback.jsonl"` (décisions humaines
en mode review).

`[outcomes]` — `journal_path` = `"state/outcomes.jsonl"`.

`[web]` — `port` = 8000 (host toujours `127.0.0.1`, jamais configurable,
ADR-09ad : l'interface web ne sert qu'une page statique, aucune logique
métier dedans).

`[jury]` (ADR-ff87, mode `selection = "jury"`) — `threshold` = 20 (écart de
score qui déclenche un débat entre juges), `quorum`, `seed` = 0, `parallel` =
5, `judges` (composition : cinq juges par défaut — retention, spectateur,
monteur, avocat, conformite — chacun avec son `usage` LLM, son `model`
strong/fast et un veto pour `conformite`).

## Modèles et consommation Claude (`[llm]`)

Tout appel LLM passe par `clipper.llm.ask(...)` (ADR-b1c1), configuré par
usage (ex. `moments`, `parts`, `vision`, `transcript_fix`, `jury_retention`) :

```toml
[llm]
backend = "claude-cli"     # ou "claude-api", "ollama"
repair_attempts = 1        # 0 = une reponse refusee echoue tout de suite

[llm.usages.moments]
model = "strong"           # niveau (strong|fast) ou nom de modele exact
```

Un usage absent de `[llm.usages]` prend le backend global et le niveau
`fast`. Par défaut (`CONFIG_DEFAULTS`), `moments` et `parts` sont en
`strong`, tout le reste en `fast`. Le backend `claude-cli` (défaut) traduit
`strong`/`fast` en `opus`/`sonnet` (`[llm.claude_cli.models]`) ; adapte ces
noms si tes modèles disponibles diffèrent.

Chaque appel réussi ou définitivement refusé est journalisé, pendant qu'une
vidéo avance, dans `workspace/<video_id>/llm_usage.jsonl` : une ligne JSON
par appel (`usage`, `model`, `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cost_usd`, `duration_s`). Avec `-v`, un résumé cumulé
par usage est aussi journalisé en fin de passage. Pour connaître l'ordre de
grandeur réel de la consommation d'une vidéo, regarde ce fichier directement
(il n'y a pas de chiffre générique fiable à donner ici : ça dépend de la
longueur de la vidéo, du nombre de clips, du mode de sélection et des
modèles choisis) :

```powershell
Get-Content workspace\<video_id>\llm_usage.jsonl | ConvertFrom-Json | Measure-Object cost_usd -Sum
```

## Diagnostic (`clipper doctor`) et modèles (`clipper models prefetch`)

`clipper doctor [--json]` (SPEC-38f7 R7, `clipper/doctor.py`) affiche un
rapport avant un premier clip, ou quand quelque chose ne marche pas : une
ligne par point (Python et version de clipper, `ffmpeg`/`ffprobe`, `claude`
trouvé et connecté ou non, Chrome, GPU, modèle mediapipe et modèle whisper
configuré présents ou non dans leurs caches, `config.toml` du dossier
courant, dossiers de données). Avec `--json`, le même rapport sort en JSON.
Code de sortie 0 si tout ce qui est requis pour un premier clip est là
(`ffmpeg`, `claude` connecté, `config.toml`, les deux modèles), 1 sinon ;
Chrome et le GPU ne sont que des avertissements (pas requis pour un premier
clip CPU).

```powershell
clipper doctor
clipper doctor --json
```

`clipper models prefetch` (`clipper/models.py`) télécharge, s'il n'y est pas
déjà, le modèle mediapipe de détection de visages et le modèle faster-whisper
configuré (`[transcribe] model`, `small` par défaut) dans leurs caches
habituels (`~/.cache/clipper/`, cache Hugging Face) — pour que le premier
`clipper run` n'attende pas un téléchargement ni n'échoue à mi-pipeline faute
de réseau. Dit pour chaque modèle s'il était déjà présent ou vient d'être
téléchargé ; un échec de téléchargement remonte explicitement (jamais de
repli, ADR-ad2e).

```powershell
clipper models prefetch
```

## Dépannage

**`ModuleNotFoundError` / erreur d'import cv2 après installation** —
installé avec `pip` au lieu de `uv`. `pip` seul ignore
`[tool.uv] override-dependencies` (qui force un seul paquet OpenCV,
`opencv-contrib-python`) et peut installer à la fois `opencv-python`
(demandé par `scenedetect`) et `opencv-contrib-python` (demandé par
`mediapipe`), qui s'écrasent l'un l'autre sur le même module `cv2`.
Réinstalle avec `uv pip install -e ".[test]"` dans un `.venv` neuf.

**faster-whisper reste sur CPU alors qu'un GPU NVIDIA est présent** — sous
Windows, CTranslate2 a besoin des paquets `nvidia-cublas-cu12` et
`nvidia-cudnn-cu12` (non installés automatiquement) et de leurs dossiers
`bin` dans le PATH au lancement. Sans ça, `clipper.gpu.get_device()` ne voit
pas CUDA et retombe sur CPU **en silence** (ce n'est pas un bug : l'absence
de CUDA est simplement non détectée). Voir *Pièges* dans `AGENTS.md`.

**Le premier `run` est lent / télécharge quelque chose** — le modèle de
détection de visages mediapipe (`blaze_face_short_range.tflite`) est
téléchargé au premier lancement dans `%USERPROFILE%\.cache\clipper\` : il
faut le réseau une fois, les lancements suivants réutilisent le cache.

**`erreur : fichier de config introuvable : ...`** — obtenu seulement avec
`--config chemin.toml` explicite pointant vers un fichier absent (jamais en
silence, voir `clipper/config.py`). Sans `--config`, l'absence de
`config.toml` n'est pas une erreur : les valeurs par défaut (`mode =
"review"`, etc.) s'appliquent. `clipper init` écrit `config.toml` (à partir
de `config.example.toml`) et `rubric.toml` dans le dossier courant — refuse
d'écraser un fichier déjà présent sans `--force`.

**`cle(s) inconnue(s) dans la section [...]`** — une clé de `config.toml` ne
figure pas dans le `CONFIG_DEFAULTS` du module correspondant (faute de frappe
ou réglage qui n'existe pas) ; le message liste les clés refusées.

**`claude` indisponible / quota dépassé** — en mode `auto`, la vidéo passe
`queued` et sera reprise par `python -m clipper queue` (ou `--watch`) une
fois `retry_at` passé ; en mode `review`, l'étape en échec remonte
directement (relance `run`/`render` une fois le problème résolu).

**Détecter une erreur de l'API Claude avant une vidéo d'1 h** — les erreurs
réelles de l'API (ex. un `400` inattendu) n'apparaissent qu'à l'usage : le
test de fumée `tests/integration/test_smoke_real.py` fait un vrai petit
appel `claude -p` pour chaque usage LLM du pipeline (`vocab`,
`transcript_fix`, `moments`, les 5 juges, `vision`, `parts`, `captions`,
`layout`, `emphasis`, `qa`), avec une entrée minuscule construite via le
vrai code de l'étape. Coût typique < 1 $ pour l'ensemble. Sauté par défaut
(consomme du quota) :

```powershell
$env:CLIPPER_CLAUDE_INTEGRATION = "1"
pytest tests/integration/test_smoke_real.py -k "not mini_video" -v -s
```

`-s` affiche la ligne `llm_usage.jsonl` (modèle, tokens, coût, durée) de
chaque appel. `tests/test_smoke_coverage.py` (toujours exécuté, sans
réseau) vérifie que la liste d'usages testés couvre bien tout le code du
pipeline, pas une liste recopiée à la main.

Option plus lourde : rejouer le pipeline complet (mode `auto`) sur un
extrait de 5 min d'une vidéo déjà présente dans `workspace/` (ffmpeg
requis) :

```powershell
$env:CLIPPER_CLAUDE_INTEGRATION = "1"
$env:CLIPPER_SMOKE_VIDEO = "workspace\<video_id>\<video_id>.mp4"
pytest "tests/integration/test_smoke_real.py::test_smoke_mini_video_end_to_end" -v -s
```
