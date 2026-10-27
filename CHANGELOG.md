# Changelog

Toutes les évolutions notables de ce dépôt sont consignées ici. Format inspiré
de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/). Depuis la 0.2.0,
le dépôt suit le [versionnage sémantique](https://semver.org/lang/fr/) :
`MAJEUR.MINEUR.CORRECTIF`, avec le plan de versions et les critères de la 1.0.0
dans [`docs/versions.md`](docs/versions.md). Tant que la version reste en `0.x`,
la configuration, les écrans et les fichiers d'état peuvent encore changer.
Notes de version détaillées : [`docs/releases/`](docs/releases/).

## [Non publié]

### Ajouté

- `clipper drain [--wait SECONDES]` et `/api/worker/drain` (POST, GET, DELETE) : mise en attente des publications avant de redémarrer `serve`. Le worker ne commence plus aucune publication (TikTok ni YouTube) ni relevé de statistiques, termine celle en cours, et `--wait` rend 0 dès que plus rien n'est piloté (non nul au délai dépassé, sans rien tuer). Un worker qui démarre lève une demande restée posée.

## [0.8.0] - 2026-10-19

Version du titre d'écran et de la publication fiabilisée : le titre d'écran d'un clip a trois styles
(`citation`, `promesse`, `promesse_emoji`), un test A/B les compare par compte TikTok avec une
rotation hebdomadaire, et les légendes suivent le jeu de la catégorie Twitch et la langue de la
vidéo. Le format `split` centre le jeu et protège la bande « Partie N ». La veille contrôle la
webcam sur les miniatures, la répartition de demain relit la config et écarte les titres trop
longs, chaque compte peut avoir son pays d'IP attendu, et la publication TikTok et YouTube résiste
à un profil Chrome tué en pleine session. Le zip s'appelle `Clipper-portable-0.8.0.zip` ; la
mise à jour se fait en relançant `Installer.bat` depuis ce zip, tes données ne sont pas touchées. Notes
détaillées : [`docs/releases/v0.8.0.md`](docs/releases/v0.8.0.md). Le mode auto de bout en bout est
repoussé à la 0.9.0. Nouveaux réglages par table : `[captions]` : `screen_title_style`,
`screen_title_max_chars` ; `[repartition]` : `title_ab_enabled`, `title_ab_start`,
`title_ab_accounts`, `title_ab_styles` ; `[reframe]` : `split_gameplay_align` ; `[render]` :
`title_split_center_y` ; `[veille]` : `webcam_check_max` ; clé de compte : `expected_country`.

### Ajouté

- Titre d'écran : réglage `[captions] screen_title_style` (SPEC-810f, qui succède à SPEC-08c2 et SPEC-6a86). `citation` (défaut, la phrase prononcée), `promesse` (annonce l'enjeu sans révéler l'issue, forme variée : une question sur deux au plus) ou `promesse_emoji` (promesse + « ... » + un seul emoji d'émotion, 🤔 refusé). Le contrat reste sobre, sans superlatif.
- Test A/B des titres par compte (SPEC-042c) : `[repartition] title_ab_enabled`, `title_ab_start`, `title_ab_accounts`, `title_ab_styles`. Un style de titre par compte TikTok, en rotation hebdomadaire ; un clip n'est planifié que sur le compte de son style (le sidecar garde `screen_title_style` et `title_shown`), un clip sans style est refusé ; le style des nouvelles vidéos est le moins fourni (`title_style` dans `pipeline.json`).
- Outil `tools/retitle_stock.py` : retitre le stock de clips prêts dans un style (`--style`, `--max-chars`), seul `screen_title` est patché ; sortie console en UTF-8 pour les emojis de `promesse_emoji`.
- Outil `tools/recaption_stock.py` : régénère la légende et les hashtags des clips prêts jamais publiés au mauvais jeu (essai à blanc, sauvegarde, rapport).
- Légendes : le prompt reçoit la catégorie Twitch du chapitre du clip ; `[captions] screen_title_max_chars` (0 = pas de limite) refuse un titre d'écran trop long (étude des vues : au-delà de 35 caractères, médiane de 80 vues contre 251).
- Split : `[reframe] split_gameplay_align` (jeu centré) et `[render] title_split_center_y` (titre posé sur la séparation), avec leurs libellés français dans la console.
- Veille : contrôle de la webcam sur les miniatures avant le choix de Claude (SPEC-fefc), borné par `[veille] webcam_check_max`.
- Comptes : champ facultatif « Pays attendu de l'IP » par compte (`expected_country`, code ISO à deux lettres), appliqué par le garde du navigateur et par le worker ; vide, le garde retombe sur `[network] expected_country`.

### Modifié

- Facecam : le prompt dit que `null` est la bonne réponse quand aucun rectangle ne montre le streamer.
- Répartition : « Recalculer » relit la table `[repartition]` de `config.toml` à chaque appel, comme la validation, au lieu de la config figée au démarrage de `serve` ; les clips dont le titre d'écran dépasse `[captions] screen_title_max_chars` sont écartés du plan (refus tracés dans `plan.title_too_long` et dans les notes, simple avertissement pour une ligne manuelle).
- Légendes : le titre d'écran et le texte d'accroche sont écrits dans la langue de la vidéo ; un validateur local fr/en refuse un titre manifestement français sur une vidéo non française.
- Conventions du dépôt : `AGENTS.md` cite SPEC-810f et SPEC-042c ; SPEC-810f, SPEC-042c et SPEC-fefc sont ratifiées ; les fixtures de `tools/ank-viz` citent SPEC-810f au lieu des specs remplacées ; `config.example.toml` documente les nouveaux réglages (la copie embarquée dans `clipper/assets/` reste identique).

### Corrigé

- Légendes : une légende « Sur X, » ou « X vu par » dont X n'est ni la catégorie Twitch ni dans le titre de la VOD est refusée (clips légendés au mauvais jeu).
- Split : un badge ou des sous-titres posés sur la bande « Partie N » sont refusés par la validation de géométrie, avec une erreur qui nomme la clé, la bande et le rectangle (« Partie 1 » s'affichait par-dessus les sous-titres).
- Render : un re-rendu forcé garde les champs de publication du sidecar (`tiktok_post`, `youtube_post`, `removed_from_platform`, `edited_at`).
- TikTok : un post programmé est retrouvé en défilant la liste Publications (virtualisée, triée par date décroissante) au lieu de conclure « à vérifier » à tort ; le premier envoi n'utilise plus l'onglet restauré par Chrome après un profil tué (`browser.fresh_page`).
- YouTube : ouvre un onglet neuf au lieu de `context.pages[0]` (connexion et publication), comme TikTok.

## [0.7.0] - 2026-10-11

Version de la console v3 : tous les écrans refaits en thème clair, avec la fiche clip en
tiroir, le calendrier de publication et la veille repensés. Elle apporte aussi la répartition
automatique du lendemain (« Plan de demain », à valider d'un clic), l'apprentissage branché sur
les relevés réels (rétention à maturité, « Ce qui marche », calibration et coach du jury), la
veille avec un style par défaut, la modération TikTok des légendes, l'amorce de gameplay des
moments et les corrections de l'audit complet du 10/10 et de la relecture du 11/10. Le zip
s'appelle `Clipper-portable-0.7.0.zip` ; la mise à jour se fait en relançant `Installer.bat`
depuis ce zip, tes données ne sont pas touchées. Notes détaillées :
[`docs/releases/v0.7.0.md`](docs/releases/v0.7.0.md). Nouveaux réglages par table :
`[repartition]` (table nouvelle) : `enabled`, `compute_time`, `max_per_source`, `bonus_points`,
`bonus_min_age_h`, `bonus_min_posts`, `exploration_per_day` ;
`[learning]` : `retention_min_n`, `breakdown_min_n`, `veille_report_min_interval_s` ;
`[moments]` : `lead_in_seconds`, `lead_in_margin_seconds`, `action_word_max_chars`,
`action_max_silent_start_s` ; `[transcribe]` : `hallucination_word_max_chars` ;
`[captions]` : `title_repair_attempts` ; `[tiktok]` : `post_lookup_interval_s`,
`post_lookup_timeout_s` ; `[browser]` : `state_dir` ; `[veille]` : `default_channel`.

### Ajouté

- Console v3 : tous les écrans sont refaits (tableau de bord, vidéos avec le détail des 13 étapes, revue, clips avec la fiche clip en tiroir, publication avec calendrier et dialogues, veille, styles, statistiques, comptes, réglages, journal). Une décision d'abord, des tiroirs et des dialogues qui gardent le contexte, la palette de commandes `Ctrl K`, les mises à jour en direct sans perdre la place dans la page, le thème clair par défaut et un affichage qui tient sur mobile (ADR-a308, SPEC-7715). Les composants (Basecoat, Idiomorph) sont copiés dans `clipper/web/static/vendor/` : aucun réseau ni build. L'ancienne feuille `style.css`, le préfixe `.v3` et les attributs `data-theme` sont supprimés.
- Moments : amorce de gameplay. `[moments] lead_in_seconds` (défaut 0, rien ne change) recule le début de chaque moment retenu pour montrer l'action qui provoque la réaction ; le recul s'arrête à la fin de la phrase précédente plus `lead_in_margin_seconds` (0,1 s), à 0 s et à la durée maximale de la grille, et la fin ne bouge jamais. Le recul est écrit dans le moment (`lead_in_s`) et journalisé.
- Légendes : la description et les hashtags n'emploient plus de terme lié à la triche ou aux sanctions (`[captions] moderation_risk_terms` : ban, cheat, hack, macro, exploit, contourner...), qui faisaient restreindre un clip aux plus de 18 ans sur TikTok. Mot entier, sans tenir compte de la casse ni des accents ; une réponse qui en contient est renvoyée au modèle, jamais corrigée en silence ; le titre d'écran n'est pas concerné. Le mot « bot » n'en fait pas partie (voie du bas en LoL).
- Veille : réglage `[veille] default_channel`, le style appliqué aux VOD mises en file depuis la veille quand aucun autre n'est choisi. Le calendrier de Publication montre « programmé » pour un post dont le créneau est futur.
- Jury (préparation de l'expérience A/B, rien n'est activé) : grille embarquée `builtin:gaming-v2` (`clipper/assets/rubric-gaming-v2.toml`, copie de `builtin:gaming` qui reste inchangée octet pour octet) avec `value` à 2, `emotion` à 2, de nouvelles questions hook / payoff / standalone portant sur le texte tel qu'il est entendu, `single_max` et `part_max` à 60 s ; signal mesuré `speech_density` (aucun appel LLM) calculé dans `moments.py` depuis les mots horodatés du moment (mots par seconde, délai du premier mot), réglé par `speech_density_min_wps`, `speech_density_max_first_word_s` et `speech_density_malus` dans la table `[bonus]` de la grille, désactivé quand les clés sont absentes (aucune grille existante ne change de note), activé dans gaming-v2 ; perspective `retention` v1 versionnée dans `prompts/jury/retention/v1.md`, non branchée ; outil `tools/replay_jury.py <posts.csv> [--rubric] [--perspective]` qui rejoue le jury sur les clips connus et écrit le Spearman entre la note et `pct_watched` par juge et pour la note finale (exécution réelle seulement avec `CLIPPER_REAL_MODELS=1`).
- Statistiques : « Ce qui marche ». `learning.breakdown(config)` (aussi rendu par `GET /api/learning`, clé `breakdown`) calcule les vues médianes à maturité, la part vue médiane et le meilleur clip par jeu (`meta.json`, sinon candidats de la veille, sinon « jeu inconnu »), streamer, heure pleine de Paris du post et compte, depuis les entrées `stats` du journal des résultats. Un groupe sous `[learning] breakdown_min_n` (défaut 5) est marqué « trop peu pour conclure » sans être masqué ; une entrée sans vues est ignorée et comptée (`skipped`), rien n'est estimé ; un fichier illisible est une erreur nommée. L'écran Statistiques affiche quatre tableaux (Jeu, Streamer, Heure, Compte) avec lien vers la fiche du meilleur clip ; aucun calcul dans la page.
- Répartition automatique du lendemain (1/5, SPEC-78dc) : bibliothèque `clipper/repartition.py`. `compute_plan` prépare, pour chaque compte TikTok actif (prêt, hors pause), les créneaux du jour (créneaux fixes gardés, grille par défaut décalée par compte, publications déjà prévues comptées) et y répartit les clips prêts jamais planifiés : meilleurs scores le soir, tourniquet entre comptes, au plus `max_per_source` clips d'un même jeu (sinon d'une même VOD) par compte et par jour, bonus de ±`bonus_points` calculé sur les vues réellement relevées seulement, un clip d'exploration par jour au plus sur un créneau hors soir. Séries en plusieurs parties et sources exclues hors du vivier. Le plan est écrit dans `state/repartition/<AAAA-MM-JJ>.json` (atomique, sous verrou, déterministe hors `computed_at`) ; `run_if_due` ne calcule qu'après `compute_time` (20:00, Paris) et jamais deux fois pour le même jour ; une erreur est écrite dans le fichier et journalisée une fois. Nouvelle table `[repartition]` documentée dans `config.example.toml`. Rien n'est créé ni envoyé : la validation viendra avec l'API et l'écran Publication.
- Répartition automatique (3/5, SPEC-78dc R8) : API du plan de demain, sans aucun calcul dans la page. `GET /api/repartition?day=` (demain, Paris, par défaut) rend le plan avec, par ligne, titre d'écran, vignette, heure de Paris et `refusal` issu de `publish.preview_series` (plafonds, avance minimale, créneau déjà pris), plus le `pool` des clips qui peuvent remplacer une ligne. `POST /api/repartition/compute` recalcule (`computed_by: "web"`) ; `PUT /api/repartition/{day}` remplace les lignes d'un ou plusieurs comptes (les lignes sont redécrites depuis le clip : score, bonus, source, exploration) et garde un `warning` visible quand une ligne dépasse `max_per_source` ou `exploration_per_day` ; `POST /api/repartition/{day}/validate` crée exactement une publication programmée par ligne (`publish.create_series`, tout ou rien par compte) : un compte en pause ou refusé arrête la validation en 409 explicite, les comptes déjà créés restent créés (`created`), le plan garde `proposed` avec `last_error`, et valider de nouveau ne crée que les comptes restants. Un plan validé répond 409 à Recalculer et à la modification. Événement SSE `repartition` à chaque changement d'un fichier de `[repartition] state_dir`.
- Répartition automatique (4/5, SPEC-78dc R9) : écran Publication, section « Plan de demain » en haut du panneau de gauche, au-dessus de « En attente ». Une ligne par créneau (heure de Paris, vignette, titre d'écran, score et bonus avec sa raison au survol, jeu ou « jeu inconnu : VOD », badges exploration et soir, refus en rouge, avertissement en orange, « aucun clip disponible » quand le vivier manque), les notes du plan telles quelles, et quatre états (proposé, validé, erreur, désactivé). « Changer le clip » (liste du vivier), « Changer l'heure » et « Retirer » envoient le PUT du compte ; « Recalculer » le POST compute ; « Valider le plan » (actif seulement avec au moins une ligne sans refus) le POST validate puis le toast « N publications créées ». Chaque requête désactive les boutons et affiche un libellé d'attente ; un événement SSE `repartition` recharge le plan. Aucune règle n'est recalculée dans la page (ADR-49cd).
- Apprentissage : rétention à maturité par clip. Chaque entrée `stats` du journal porte la durée du clip (sidecar), `pct_watched` (part moyenne vue, null si la durée ou le temps moyen manque) et la source du moment (`transcript` ou `action`, null si inconnue). L'écran Statistiques affiche un tableau trié par part vue, avec un message « n = X, trop peu pour conclure » sous le réglage nommé `[learning] retention_min_n` (défaut 30). Aucune corrélation calculée.
- Répartition automatique (2/5, SPEC-78dc R7) : le worker appelle `repartition.run_if_due` à chaque tour, juste après l'apprentissage ; `[repartition] enabled = false` ne fait rien, et une erreur de répartition est journalisée une fois sans arrêter le worker.
- Répartition automatique (5/5, SPEC-78dc R10) : README et CHANGELOG. Le README documente la section « Plan de demain » (calcul à `compute_time`, 20:00 Paris ; modification ; validation obligatoire, rien n'est créé ni envoyé sans clic ; règle « jeu inconnu : regroupé par VOD ») et la table des réglages `[repartition]`. Les défauts cités sont comparés au dict `CONFIG_DEFAULTS` de `clipper/repartition.py` par `tests/test_readme_assets.py`. Aucune capture d'écran pour l'instant : l'écran Publication sera capturé plus tard.
- Jury : traçabilité de la version de perspective. Chaque juge de `moments.json` (`jury.judges[]`) porte `perspective_sha`, les 12 premiers caractères hexadécimaux du sha1 du texte exact de sa perspective (UTF-8) ; le texte n'y est ni copié ni haché autrement. `learning.sync` recopie dans chaque nouvelle entrée `stats` du journal un dict `perspectives` = `{juge: perspective_sha}`, lu dans le `moments.json` du clip ; absent (moments.json ancien, moment sans jury), la clé est omise, jamais inventée. Aucun changement de note ni de prompt.

### Modifié

- Console : les en-têtes d'écran sont homogènes et l'écran Statistiques s'ouvre sur le dernier compte consulté, sinon le premier qui a des vues.
- Tests : la suite complète est plus rapide (environ x1,5), sans retirer ni affaiblir aucun test : fixtures de reframe réduites là où la résolution n'est pas l'objet, générateurs ffmpeg de test_qa en cache, test du scan sans chronomètre.
- Dépôt : les fichiers copiés dans `vendor/` sont marqués binaires (`.gitattributes`), leurs sha256 sont stables quel que soit `autocrlf`.
- Répartition (SPEC-78dc R6) : un clip d'exploration atteint vraiment la publication. Jusqu'ici `exploration_per_day` était un plafond jamais atteint : un clip d'exploration (score bas par construction) ne passait jamais devant les autres du vivier. Désormais, avant le remplissage par score, `min(exploration_per_day, clips d'exploration disponibles)` créneaux sont réservés, un par compte et par jour au plus, sur le premier créneau hors soir du compte, avec le meilleur clip d'exploration que ce compte peut prendre (tourniquet des comptes). Le plan note « exploration : <clip> sur <compte> à <heure> » ; sans créneau hors soir, rien n'est réservé et une note le dit. Sans clip d'exploration, le plan est inchangé.
- Répartition : le bonus de source ne compte que les posts relevés d'au moins `bonus_min_age_h` heures (défaut 24, nouveau réglage `[repartition]`, validé entier >= 0 dans `_settings`), référence comprise : à 12 h un post n'a que 45-95 % de ses vues finales, et une source publiée récemment était pénalisée (ex. 100-200 vues à 1-2 h contre 2 400 à 26 h). `bonus_min_posts` passe de 2 à 3 (avec 2 posts, un seul mauvais créneau fait la médiane). `bonus_reason` dit l'âge minimal retenu. Tests CPU sans réseau dans `tests/test_repartition.py`.
- README : captures refaites en thème clair sur la vraie console (plus de variante sombre ni de `<picture>`), vignettes, titres de VOD et noms tiers floutés ; nouveaux écrans Veille, fiche clip et bloc « Rétention à maturité » ; l'écran Comptes n'est plus montré (e-mails) ; `tests/test_readme_assets.py` n'exige plus de variantes sombres.
- README refondu (même style, plus court) : à jour du code réel (13 étapes dont `action`, styles et formats gaming, jury et apprentissage, veille, YouTube Shorts, installeur portable) ; captures existantes gardées.
- Veille : `extract_video_id` et `DownloadError` vivent dans `clipper/workspace.py` (module sans étape) ; `clipper/download.py` les réexporte, et la veille n'importe plus l'étape download (ADR-ca9a).
- Web : la documentation des défauts du formulaire des styles vient de `clipper.config` (`_defaults_documentation`) ; `clipper/web` n'importe plus dynamiquement de module d'étape, et le test de garde ADR-49cd signale tout import dynamique, `getattr` ou `sys.modules` visant une étape (ADR-49cd).

### Corrigé

- Reframe `stream_split` : l'ajustement au format du panneau rogne à l'intérieur de l'incrustation de la webcam, plus de bande de jeu en haut du panneau. Une seule période avec candidats ne bloque plus la vidéo sur « aucune webcam », la persistance d'un candidat compare aussi le contenu (écran d'attente contre vraie webcam), et une période unique n'a plus de garde-fou contre la réponse de Claude.
- Presets : la lecture-modification-écriture d'un preset se fait sous verrou (API web, worker, migration), deux écrivains ne s'écrasent plus.
- Worker : un worker orphelin est visible, la reprise d'une vidéo se fait sans yt-dlp quand le fichier est là, `retry_at` null ne casse plus la boucle, un seul flux temps réel. Une seule instance et une seule entrée `running`, Annuler et l'arrêt de `serve` tuent tout l'arbre de processus, un `Ctrl+C` dans la console de `serve` n'est plus pris pour un worker refusé, la reprise des vidéos en file passe par la file, et le worker relit `config.toml` (par date de modification) avant la répartition.
- Audit complet du 10/10 : jury (contrôles stricts, poids bornés, juge à veto obligatoire, dédoublonnage par format), parts (accroche de `moments.json` conservée, candidats d'action jamais rejetés), publication (une programmation « à vérifier » n'est plus republiable), répartition (la validation rejoue les exclusions), download (fichier vérifié par ffprobe, fichier refusé supprimé pour que la relance retélécharge), audio lu par blocs, scenes (écriture d'image vérifiée, `--force` vide `frames/`), web (garde locale de l'API, SSE sans parcours des profils Chrome, hub qui survit à un scan en échec, écran Statistiques de nouveau alimenté), écriture de config sûre face aux écrivains concurrents, installeur (désinstallation depuis `app`, prévol avant `.venv`), rendu, QA, sous-titres et `doctor`.
- Apprentissage : rattachement post → clip symétrique (égalité exacte avant préfixe, minute puis fenêtre, posts supprimés exclus), `pct_watched` borné, jeu lu dans `seen.json` et métrique vérifiée avant écriture.
- Dépôt public : mentions personnelles retirées, listes de garde en sha256, hashtags stricts, doublon CUDA et classe morte supprimés.
- Répartition web : le vivier de l'écran et le PUT appliquent aussi les exclusions R2 (SPEC-78dc R2). Une seule règle, `clip_exclusion` (série en plusieurs parties, source de `excluded_sources`, vidéo dans la file de traitement), sert au vivier, à `compute_plan` et au PUT ; le PUT refuse en 422 avec la raison et le fichier du jour reste inchangé. Avant, un clip banni ou dont la vidéo était en traitement pouvait être glissé à la main dans le plan.
- Coach du jury : chaque cas est rejoué avec la grille qui l'a noté (`rubric` de son `moments.json`), plus avec celle de `[moments] rubric_path` ; les cas sont regroupés par grille, une leçon et un rejeu par juge et par grille, avec leurs critères et poids. Un clip dont la grille est absente ou illisible est écarté (`skipped` dans `coach.json`, avertissement journalisé), jamais rejoué avec une autre grille. Le rejeu avant/après ne se fait plus sur les cas qui ont écrit la leçon : leçons = les pires cas, validation = d'autres cas de la même grille, disjoints, tirés à graine fixe (`[jury_coach] validation_cases`, `validation_seed`), au moins `min_cases` sinon aucune proposition (raison écrite) ; `vN.md` écrit `lecons` et `validation`.
- Apprentissage : la calibration et le coach du jury apprennent sur la même vérité. `learning.sync` écrit aussi `pct_watched` dans `stats` (la clé au niveau de l'entrée reste pour l'écran), donc `[jury_calibration] stats_metric = "pct_watched"` ne donne plus `no_metric` partout ; `jury_coach` lit `stats_metric` au lieu de `views_percentile` en dur ; si des entrées `stats` existent mais qu'aucune ne porte la métrique choisie, la calibration échoue visiblement (`sync.json` `last_error`) au lieu d'un simple avertissement. Valeur par défaut de `stats_metric` inchangée.
- Apprentissage : un clip publié sans statistique ne compte plus comme « résultat parfait » dans la calibration du jury. `learning.sync` ne passe à la calibration que les clips qui ont une entrée `stats` (un clip immature ou d'un compte sous `min_account_posts` en est écarté), et `jury_calibration` utilise la statistique seule (`views_percentile`, `pct_watched`) comme résultat du moment, sans y mélanger le `qa` constant des clips publiés ; un moment sans statistique est exclu.
- TikTok : un post programmé vu après le dernier relevé complet (page Publications opportuniste) n'est plus déclaré « supprimé de TikTok » : `deleted_post_ids` ne retranche que les posts vus au plus tard au dernier relevé complet. Il n'était donc plus listé dans Statistiques, sa fiche renvoyait « introuvable » et les alertes 0 vue le sautaient (18 faux « supprimés » sur 21 le 09/10). Une vraie suppression est toujours détectée.
- TikTok : un post pas encore en ligne (date de publication future) n'est plus lu en détail : il n'enregistre plus de `avg_watch_s 0.0`, `watched_full 0.0`, `new_followers 0` inventés (ADR-ad2e), il porte `detail_not_read`.
- Apprentissage : `state/veille/bilan.json` n'est plus réécrit à chaque tour du worker (toutes les 2 s). `write_veille_report` n'écrit le fichier que si son contenu hors `computed_at` change (relu sous le verrou) et rend le rapport dans tous les cas ; `run_if_due` ne le recalcule qu'au plus une fois par `[learning] veille_report_min_interval_s` (défaut 60) ou dès qu'un relevé vient de tourner. Fin des événements SSE `veille` en continu sans changement réel.
- Worker : une exception inattendue (`KeyError`, `TypeError`, `AttributeError`) sous l'apprentissage, la répartition ou la surveillance ne fait plus sortir `tick()` (worker mort, plus aucune publication) : elle est journalisée une seule fois, et écrite dans `sync.json` `last_error` pour l'apprentissage.
- Worker : le rapprochement des programmations compare l'heure réellement programmée par TikTok (`tiktok_publish_at`, enregistrée aussi pour une entrée « à vérifier »), et accepte à défaut un écart d'au plus 5 min (pas du sélecteur) avec une légende unique ; deux posts à la même légende dans la fenêtre ne concluent rien.
- Répartition web (SPEC-78dc) : quatre défauts de l'écran « Plan de demain ». Le vivier est rendu par compte (`pool` sous chaque compte, `repartition.account_pool`) : un clip validé pour un autre compte n'est plus proposé, et `PUT /api/repartition/{day}` refuse en 422 (fichier inchangé) une ligne dont le clip est validé pour un autre compte. Le `PUT` refuse aussi en 422 un créneau dont le jour (heure de Paris) n'est pas celui du plan ; le sélecteur d'heure de la page est borné à ce jour. Les parties d'une série en plusieurs parties, déjà exclues du plan, ne sont plus proposées dans le vivier et un `PUT` d'une telle partie répond 422 `multi_part_series`. Après une validation partielle, les avertissements « plus de N clips de la même source » ne comptent plus deux fois les lignes des comptes déjà créés (comptées par leurs publications) et ces lignes n'ont plus d'avertissement.
- Répartition : le jeu d'une VOD est aussi lu dans les candidats de la veille (`state/veille/days/<jour>.json`), pas seulement dans `seen.json` (`queued`, toujours prioritaire). Une VOD mise en file à la main (hors bouton Clipper de la veille) n'est plus classée « jeu inconnu : VOD » alors que la veille la nomme. Cas réel du 09/10 : une VOD AION 2 sortait en « vod: ». Si plusieurs jours donnent un jeu différent pour une même VOD, le jour le plus récent gagne. Un fichier de jour illisible est ignoré avec une note dans le plan, jamais d'arrêt ni de jeu inventé.
- Écran Vidéos : « Purger les vidéos terminées » réagit au clic. Le bouton est désactivé et affiche « Calcul de l'espace libérable… » pendant le calcul (rétabli en cas d'erreur ou d'annulation), puis « Purge en cours… » pendant la purge, sans possibilité de lancer deux purges à la fois. Le calcul de taille côté serveur parcourt chaque dossier une seule fois (`os.scandir`) pour le même total.
- Publication TikTok : une vérification de contenu jamais terminée (délai `[tiktok] content_check_timeout_s` dépassé) ne met plus tout le compte à l'arrêt. Le clip passe en échec avec une raison explicite (« choisis un autre clip ou réessaie plus tard »), le compte reste « prêt à publier » et l'entrée due suivante part. Garde-fou : si deux clips de suite (même compte) échouent ainsi, le compte est arrêté comme avant, raison écrite (le problème vient alors sans doute du compte). Les autres arrêts (captcha, connexion, refus, page inattendue…) arrêtent le compte comme avant. Cas réel du 09/10 : un clip bloqué deux fois, les autres posts du compte passaient.
- Publication TikTok : le rapprochement post ↔ clip vérifie aussi l'heure. Deux posts à légende commune (la liste de TikTok Studio tronque les légendes) ne peuvent plus échanger leurs créneaux : un post n'est retenu que si sa date de publication est le créneau de l'entrée, à la minute (heure de Paris). Aucun post ou plusieurs posts à la même heure : rien n'est conclu (« à vérifier » reste, journal info ou warning une fois). Sur la page Publications, plusieurs liens à la légende commune : celui dont l'heure affichée est le créneau ; sinon le premier lien, ambiguïté journalisée. Légende unique sans heure connue : comportement inchangé.
- Publication TikTok : après une publication prouvée, le nouveau post n'est pas encore listé sur la page Publications (3 programmations sur 11 finissaient à tort « à vérifier »). La page est maintenant relue (rechargée) tous les `[tiktok] post_lookup_interval_s` (défaut 10 s) pendant au plus `[tiktok] post_lookup_timeout_s` (défaut 60 s, 0 = une seule lecture), arrêt dès que le post est trouvé, nombre d'essais et délai journalisés ; sans post après le délai, comportement inchangé (« à vérifier », jamais « publié » sans id). Le worker rapproche aussi automatiquement une entrée « à vérifier » dont la légende apparaît dans un relevé complet postérieur : elle repasse en programmée sur TikTok avec l'id et l'adresse du post, sans reprogrammation, journal une fois.
- Publication TikTok : une programmation dont aucun id de post n'est retrouvé sur TikTok Studio n'est plus enregistrée comme publiée ; l'entrée passe en échec « à vérifier » (compte non arrêté, jamais reprogrammée seule, risque de doublon indiqué), avec journal et évènement console. Au relevé complet suivant, une entrée « programmée sur TikTok » sans id que le relevé ne montre pas est signalée une fois (note de l'écran Publication, journal). Cas réel : un post programmé 05 pour 17 h, déclaré « programmé » alors qu'il n'existait pas.
- Moments : un passage d'action prend comme parole et comme accroche les mots horodatés compris dans le passage, même quand leur phrase commence avant ou déborde après (mots géants exclus) ; une phrase sans horodatage des mots ne fournit son texte que si elle est entièrement incluse. Sans aucun mot retenu, l'accroche reste la description de l'image.
- Publication : un report impossible sur une entrée due (ex. plafond d'un compte sans créneau libre) ne fait plus sauter les autres entrées dues du même passage du worker ; l'entrée en cause attend avec la raison visible et un avertissement journalisé.
- Fiche clip : l'historique des statistiques ne garde qu'un relevé par changement de valeurs (vues, likes, commentaires), le dernier relevé est toujours gardé ; la date « Publié le » devient « Envoyé à TikTok le » (le créneau reste « Créneau ») ; les valeurs des panneaux ne débordent plus de leur bord.
- Apprentissage : un moment sans champ `source` dans `moments.json` est un moment de transcription ; la source des entrées `stats` n'est plus nulle pour les chaînes en `[moments] candidates = "transcript"`. Les entrées déjà écrites ne sont pas réécrites.
- Installeur : un pointeur `%LOCALAPPDATA%\Clipper\install.json` dont le champ `app` contient des caractères interdits dans un chemin (ex. `C:\a|b<x>`) ne fait plus planter la désinstallation (erreur .NET brute, exit 1, avant toute suppression, y compris en `--dry-run`) : il est laissé avec la raison « champ app illisible (...) » et le remède affiché, et la désinstallation continue.
- Publication : les captures d'arrêt TikTok et YouTube (captcha, vérification, connexion, page inattendue) sont écrites sous le dossier `[browser] state_dir` réglé, comme la route qui les sert ; avant, elles partaient toujours dans `state/browser` et la capture affichait un 404. Défaut inchangé.
- Installeur : la désinstallation supprime le pointeur `%LOCALAPPDATA%\Clipper\install.json` de cette installation (seulement s'il désigne le dossier app désinstallé, comparaison de chemins normalisée). Un pointeur d'une autre installation ou illisible est laissé et le dit ; `%LOCALAPPDATA%\Clipper` n'est supprimé que s'il est vide. `--dry-run` liste le pointeur (supprimé ou laissé, avec la raison). Le dossier de données n'est pas touché.
- Transcription : les « mots » géants inventés par whisper sur la musique (ex. « Tantantan… » de 420 caractères) sont retirés juste après whisper, avant la correction LLM : texte du segment reconstruit, segment vide retiré, chaque retrait journalisé (timecode, longueur, 40 premiers caractères) et compté dans `hallucinated_words_removed` de `transcript.json`. Seuil : réglage nommé `[transcribe] hallucination_word_max_chars` (défaut 40, entier >= 1, sinon erreur). Les transcripts déjà faits ne sont pas retouchés.
- Config : plus de dossiers codés en dur. Le worker construit `--config` depuis `[watch] presets_dir`, le serveur web dérive dossier des styles et fichier de base de `[watch] presets_dir` / `base_config`, son flux temps réel surveille les dossiers de `[worker] queue_path`, `[publish] state_dir` et `[watch] state_dir`, et les profils navigateur vivent sous le nouveau réglage `[browser] state_dir` (défaut `state/browser`, comportement identique).
- Moments : un passage d'action dont la vraie parole commence trop tard est rejeté, avec le délai dans la raison (liste des rejetés de `moments.json`). La parole est mesurée sur les mots horodatés du transcript (plus sur les segments) ; un mot sans espace de plus de `[moments] action_word_max_chars` caractères (défaut 40, hallucination whisper sur la musique) est ignoré ; un premier mot retenu après `[moments] action_max_silent_start_s` secondes (défaut 5) du début du passage rejette le candidat. Un passage sans aucun mot retenu garde la règle « sans parole » (accroche = image). `action.json` inchangé.
- Moments : une phrase déjà commencée avant un passage d'action et qui continue dedans compte comme parole dès le début du passage (mots horodatés : premier mot retenu à partir du début du passage ; sans horodatage : parole présente au début). Plus de rejet à tort « la parole commence trop tard » ; l'accroche (`hook_text`) ne change pas. Idem pour une phrase qui commence dans le passage et finit après sa fin : elle compte pour la mesure de la parole.
- Interface : le flux temps réel n'émet plus jamais un même fichier deux fois (dossiers comparés en chemins résolus) ; un fichier sous `[publish] state_dir` ou `[watch] state_dir` n'est émis qu'avec son genre fixe, même si la racine de la file le contient.
- Apprentissage : un journal des résultats illisible (ligne tronquée, erreur de lecture) est une `LearningError` qui nomme le fichier ; `GET /api/learning` répond 422 avec ce message au lieu d'un 500.
- Légendes : `[captions] title_repair_attempts` est validé en tête d'étape (entier >= 0, booléen refusé) ; une valeur invalide est une `CaptionsError` qui nomme le réglage, sans plus tronquer 2.7 en silence.
- Captions : un titre d'écran trop long (ex. 7 mots pour 5 au plus) n'arrête plus la vidéo entière après une seule réparation. L'appel qui produit titre d'écran et légende dispose de ses propres essais de réparation, réglage `[captions] title_repair_attempts` (défaut 3) ; les autres usages LLM gardent `[llm] repair_attempts`. Aucun titre de secours : après les essais, l'erreur remonte comme avant.

## [0.6.0] - 2026-10-08

Version autour des formats stream et webcam (zoom, contrôle par clip, visage dans le
recadrage, cas « aucune webcam »), des styles gaming (candidats d'action), de la fiche
clip, de l'alerte « 0 vue à 24 h », du préchargement du téléchargement, de la publication
TikTok (fenêtres superposées, interrupteur de vérification de contenu, post supprimé de la
plateforme), de la veille (historique de tendance, Steam officiel, calendrier des sorties)
et de la vitesse (étape scenes, téléchargement, suite de tests). Le zip s'appelle
`Clipper-portable-0.6.0.zip` ; la mise à jour se fait en relançant `Installer.bat` depuis
ce zip, tes données ne sont pas touchées. Nouveaux réglages par table :
`[worker]` : `prefetch_download`, `prefetch_min_free_gb` ;
`[download]` : `concurrent_fragments`, `fragment_retries`, `ffmpeg_bin` ;
`[scenes]` : `detect_parallel`, `detect_chunk_seconds`, `analysis_skip_loop_filter`,
`extract_batch`, `extract_threads` ;
`[reframe]` : `facecam_clip_face_margin`, `facecam_clip_face_crop_height`,
`facecam_clip_face_min_inside`, `facecam_clip_face_min_share`, `facecam_zoom_frames`,
`facecam_zoom_tile_height`, `facecam_face_stable_share` ;
`[tiktok]` : `click_timeout_s` ;
`[learning]` : `zero_view_alert_hours`, `zero_view_alert_max_views`,
`zero_view_alert_account_min`, `coach_min_new_cases`, `coach_min_interval_days` ;
`[action]` (table nouvelle, désactivée par défaut) : `enabled`, `max_passage_seconds`,
`frames_per_passage`, `max_images_per_hour` ;
`[moments]` : `candidates`, `action_snap_seconds` ; `[gate]` (table optionnelle d'une
grille : `criterion`, `min`, `unless_criterion`, `unless_min`) ;
`[veille]` : `twitch_access_workers`, `twitch_access_attempts`, `twitch_access_retry_pause_s`,
`veille_deadline_s`, `trend_days`, `trend_games_max`, `steam_reviews_*`, `twitch_history_*`,
`steam_players_lookups_max`, `steam_followers_lookups_max`, `steam_followers_pause_s`,
`steam_followers_retry_max`, `steam_followers_retry_wait_max_s`, `community_min_steam_players`,
`community_min_steam_followers`, `community_min_twitch_viewers`, `community_min_hypes`,
`max_vods_per_game`, `llm_retry_delay_min`, `llm_retry_max` ; `[veille] igdb_min_hypes` vaut
5 par défaut, et `twitch_access_check_max` et `igdb_releases_max` sont retirés (ignorés
s'ils restent dans `config.toml`). La veille reste désactivée par défaut.

### Ajouté

- Fiche par clip : une page (`#/clip/<video_id>/<clip_id>`, lien « Fiche complète » dans le tiroir de l'écran Clips) qui rassemble le clip (titre, score et critères, raison, passage dans la VOD, QA), le jury, la publication (compte, statut, créneau, lien du post) et les relevés TikTok du post. `GET /api/clips/{video_id}/{clip_id}/sheet` est en lecture seule. Une vidéo supprimée (`.mp4` absent) garde la fiche avec la mention « vidéo supprimée, fiche conservée » ; une donnée absente s'affiche « inconnu », jamais 0.
- Apprentissage : alerte « 0 vue à 24 h » sur le tableau de bord (section « Posts à 0 vue ») et une ligne WARNING
  par post (`state/learning/zero_views.json`, une seule fois). Lit les relevés TikTok déjà faits, sans réseau :
  un post en ligne depuis `zero_view_alert_hours` (24) dont le dernier relevé donne au plus
  `zero_view_alert_max_views` (0) vue est signalé ; `zero_view_alert_account_min` (2) posts en alerte d'un même
  compte donnent une alerte au niveau du compte. Un post sans relevé après le délai est rendu à part (« Pas de
  relevé »), jamais compté à zéro. Les posts supprimés de la plateforme et les comptes en pause sont ignorés.
- Publication : action « Supprimé de la plateforme » pour un post supprimé à la main de TikTok ou YouTube
  (programmé ou déjà en ligne). `POST /api/publish/{video_id}/{clip_id}/removed` (raison facultative) et bouton
  avec confirmation dans la fiche du clip (écran Publication) passent l'entrée en `removed_from_platform`
  (`removed_at`, `removed_reason`, journalisé) ; Clipper n'efface rien sur la plateforme. L'entrée libère son
  créneau et ne compte plus pour les plafonds, n'est jamais republiée, reste visible dans la liste « Supprimés de
  la plateforme » et supprimable par « Supprimer la sélection ». Le sidecar porte `removed_from_platform` :
  `clipper.learning` n'y rattache aucun post, n'en verse ni résultat ni statistiques, ne le compte ni en
  calibration ni dans le bilan veille (jamais un résultat à 0 vue). Refus 409 sur une entrée non publiée ou en cours.
- Préchargement du téléchargement : dès que la vidéo en cours a fini son étape download et passe aux étapes
  CPU, le worker télécharge (étape download seule, `python -m clipper download <url>`) la première VOD en
  attente de la file, au plus une à la fois et jamais deux traitements en parallèle. Quand elle devient la vidéo
  en cours, son download déjà fait n'est pas relancé. Réglages `[worker] prefetch_download` (défaut vrai ; faux =
  comportement d'avant) et `prefetch_min_free_gb` (défaut 60 : sous cet espace libre sur le disque du workspace,
  pas de préchargement, une ligne de journal). Un échec est journalisé et visible (étape download et vidéo
  `failed` dans `pipeline.json`) sans toucher la vidéo en cours ; son download est retenté quand elle passe en
  cours. `/api/queue` expose `prefetch` (`running`, `done` ou `failed`) sur l'entrée en attente. Retirer ou
  annuler l'entrée, ou arrêter le worker, termine le processus de préchargement ; au démarrage, un préchargement
  resté d'un worker arrêté est terminé.
- Clips : bouton « Supprimer la sélection » (style danger) dans la barre de sélection, après confirmation
  (« Supprimer N clips ? Irréversible. »). `POST /api/clips/delete` supprime via
  `clipper.workspace.delete_clips` le `.mp4`, le sidecar `.json` et les annexes d'un clip jamais publié, rend les
  octets libérés. Un clip déjà publié n'est plus refusé : son `.mp4` et ses annexes lourdes sont supprimés,
  son sidecar `.json` est gardé intact (lien post → clip, statistiques, apprentissage du jury) ; la réponse
  distingue `deleted` (supprimés entièrement) et `video_deleted` (vidéo seule), le journal aussi. Un clip
  programmé, en cours ou en attente reste refusé. Tout ou rien par série (une partie choisie entraîne toute sa
  série, refusée entière si une partie est programmée, en cours ou en attente ; sinon chaque partie suit sa
  propre règle) ; chaque refus est rendu avec sa raison. La confirmation dit « Les clips publiés gardent leurs
  infos (stats), seule la vidéo est supprimée. » et le toast compte supprimés, vidéos supprimées et refusés.
  Un clip publié sans vidéo (`video_deleted` dans `GET /api/clips`) reste listé, marqué « Vidéo supprimée »,
  ni sélectionnable ni lisible ; sa fiche n'a plus ni lecteur, ni re-rendu, ni téléchargement, et
  approve / re-rendu / changement de titre répondent 409 « vidéo supprimée » plutôt qu'une erreur 500.
- Comptes : pause manuelle d'un compte (SPEC-f348). La case « Prêt à publier » devient cliquable : cochée, un
  clic met le compte en pause (« En pause (manuel) depuis le … », heure de Paris) ; en pause, un clic le
  reprend (connexion revérifiée). Un compte en pause n'est jamais proposé dans Clips ni Publication, et une
  publication existante affiche « (en pause) » ; ses entrées restent en attente avec la raison, les
  publications déjà programmées côté plateforme ne sont pas touchées.
- Jury action (6/6) : preuve de bout en bout et documentation (SPEC-b0f3 R15, R17, R18). Deux tests de
  `tests/test_pipeline.py` (FakeBackend, source synthétique) : un style gaming action enchaîne audio avant
  scenes (`peak_windows`), action avant moments, produit `action.json` puis un `moments.json` avec un moment de
  source « action » et le monologue rejeté par le seuil éliminatoire ; le style témoin (`builtin`,
  `transcript`) garde `scenes.json` identique octet pour octet, un `action.json` vide, aucun appel `action`.
  `tests/test_action_real.py` : test réel optionnel sur un extrait de VOD (`CLIPPER_ACTION_REAL=1`,
  `CLIPPER_ACTION_REAL_VIDEO`), jamais lancé par défaut. README (étape `action`, réglages `[action]`,
  `candidates`, clés des deux presets), AGENTS.md et GUIDE (13 étapes) à jour.
- Jury action (5/6) : la console gère les styles gaming action (SPEC-b0f3 R16). Grille « Gaming action »
  (`builtin:gaming-action`, reconnue aussi pour un fichier au contenu identique) dans le choix de grille ;
  `[moments] candidates` (transcript / transcript+action) et `action_snap_seconds` dans le formulaire ; section
  « Action (passages de jeu) » pour la table `[action]` (aide tirée de `CONFIG_DEFAULTS`), enregistrée sans perdre
  de clé ; la fiche vidéo (jury par moment) indique la source de chaque moment (transcription ou passage d'action)
  quand `moments.json` la porte, inchangée sinon. Aucune logique de traitement dans `clipper/web`.
- Veille : historique de tendance sur 30 jours dès le premier relevé pour chaque jeu suivi (SPEC-85a0). Deux
  nouvelles sources, « Steam (avis 30 j) » (histogramme des avis, endpoint non documenté) et « Twitch (VOD 30 j) »
  (Helix *Get Videos*, plafond 500 VOD donc jours anciens inconnus), et des courbes par jeu : colonne « 30 j » de
  « Ce qui monte » et carte de proposition (ligne SVG sans bibliothèque, un trou pour un jour sans mesure, « n j
  mesurés / 30 », « pic <date> », résumé « s4 → s1 »). Onze réglages `[veille]` (`trend_days`, `trend_games_max`,
  `steam_reviews_*`, `twitch_history_*`, `twitch_access_attempts`, `twitch_access_retry_pause_s`,
  `veille_deadline_s`), les sept principaux dans le formulaire des réglages et l'aperçu de l'écran Veille.
- Jury action (1/6) : la planche d'images légendée de `vision.py` devient la bibliothèque
  `clipper/montage.py` (`montage(...)`, `LABEL_HEIGHT`, `MontageError` nommant le fichier), réutilisable par
  l'étape action sans qu'une étape en importe une autre (ADR-b16b, SPEC-b0f3 R8). `vision` l'importe, rendu
  identique, aucun changement de comportement.
- Jury action (2/6) : grille embarquée `builtin:gaming-action` (`[moments] rubric_path`, action 5,
  émotion 3, `min_score` 50, clips de 20 à 90 s) et table optionnelle `[gate]` (seuil éliminatoire :
  `criterion`, `min`, `unless_criterion`, `unless_min`) validée par `load_rubric` et appliquée par
  l'étape moments avant `min_score`, y compris à la re-notation après vision. `builtin` et
  `builtin:gaming` restent identiques octet pour octet ; une grille sans `[gate]` ne change rien.
- Jury action (3/6) : nouvelle étape `action` (`clipper/action.py`, table `[action]`, désactivée par défaut) entre
  `scenes` et `moments`. Elle détecte sans LLM des passages d'action (pics audio + densité de changements de plan
  par fenêtres de 30 s, fusion, coupe à `max_passage_seconds`, plafond par heure), choisit
  `frames_per_passage` images déjà extraites par `scenes` (aucun ffmpeg), les fait décrire par planches
  (usage `action`, modèle rapide, plafond `max_images_per_hour`, reprise par `action_partial.json`) et écrit
  `action.json`. `audio` tourne désormais AVANT `scenes` (`pipeline.STEPS`) ; `scenes` reçoit `peak_windows`
  (positionné par le pipeline d'après `[action] enabled`) : à `false`, `audio.json` est ignoré et `scenes.json`
  reste identique octet pour octet ; à `true`, il élargit les fenêtres décodées aux pics hors parole et porte
  `"peak_windows": true`. Un `pipeline.json` antérieur sans l'étape `action` est relu (étape en attente).
  Activer `[action]` sur une vidéo déjà analysée demande `--force` sur `scenes` (erreur explicite sinon).
- Jury action (4/6) : `[moments] candidates = "transcript+action"` (défaut `"transcript"`, sorties et prompts
  inchangés) ajoute un candidat par passage de `action.json`, borné sur la frontière de phrase à moins de
  `action_snap_seconds` (3 s) sinon au centième, connecteurs de tête retirés, SponsorBlock, durée et
  dédoublonnage comme les autres. Même proposeur (qui voit la liste des passages), même jury (aucun appel de
  plus) ou, en sélection `single`, un appel `moments` de plus qui note les seuls candidats d'action ; la matière
  donnée aux noteurs est la parole (ou « (aucune) »), les signaux et les images décrites. Bonus, `[gate]`,
  `min_score`, non-chevauchement et plafond sont communs. `moments.json` porte `source` et, pour l'action, le
  bloc `action` ; la re-notation après vision les conserve. `action.json` absent ou `[action] enabled = false`
  avec `transcript+action` : erreur explicite, jamais de repli sur la transcription.
- Apprentissage (3/4) : le coach des prompts du jury passe tout seul dans le worker quand
  `[learning] coach_min_new_cases` (10) clips mûrs nouveaux existent et que `coach_min_interval_days`
  (7) jours se sont écoulés ; ses propositions sont consignées dans `state/learning/coach.json` et
  ne s'appliquent jamais seules. L'écran Statistiques gagne une section « Apprentissage » (état de la
  boucle, poids par juge, propositions avec boutons Adopter, qui écrit la perspective dans
  `config.toml`, et Refuser) ; routes `GET /api/learning` et `POST /api/learning/coach/<juge>/<version>/adopt|refuse`.
- Veille : calendrier des sorties (SPEC-df51). La section « Sorties de jeux » devient un calendrier :
  bandeau des sorties récentes en cartes à jaquette (pastille « Aujourd'hui » / « Sortie J+N »,
  « Tendance », plateformes, « Portage », hypes, ligne de tendance, puce « Communauté » ou
  « Peu de monde »), frise « À venir » d'une colonne par jour, liste par jour sur téléphone et panneau
  de détail (toutes les plateformes, tendance, courbe des joueurs, lien « Voir sur IGDB »). Les
  jaquettes sont chargées par le navigateur depuis `images.igdb.com` ; Clipper ne les télécharge ni ne
  les stocke. Réglages › Veille : `igdb_recent_max`, `igdb_upcoming_max` et les réglages de
  communauté et de diversité ci-dessous.
- Veille : « Ce qui monte » gagne les colonnes « Abonnés Steam » (avec le gain sur 7 jours ou
  « historique insuffisant ») et « Communauté » (« ok » ou « insuffisante »), nomme le chiffre Steam
  « pic du jour » ou « à l'instant », et dessine une mini-courbe des joueurs par jeu (pic du jour et
  instantané en deux couleurs ; « 1 jour de mesure » tant qu'il n'y a pas d'historique). Le bandeau
  des sources montre « Steam (joueurs hors top) » et « Steam (abonnés) », et les KPI du jour comptent
  les VOD écartées pour communauté insuffisante ou jeu inconnu.
- Veille : Steam officiel à la place de SteamDB, filtre de communauté et diversité (TASK-82da). Les
  joueurs simultanés et le pic du jour viennent de l'API Steam officielle (top 100, puis un appel par jeu
  hors top, plafonné), les abonnés de la page publique `memberslistxml` (appels espacés). Une VOD n'est
  proposée que si son jeu atteint l'un des quatre seuils de communauté (joueurs Steam, abonnés Steam,
  viewers Twitch FR, hypes IGDB), et au plus `max_vods_per_game` VOD par jeu. Nouveaux réglages
  `[veille]` : `steam_players_lookups_max`, `steam_followers_lookups_max`, `steam_followers_pause_s`,
  `community_min_steam_players`, `community_min_steam_followers`, `community_min_twitch_viewers`,
  `community_min_hypes`, `max_vods_per_game`, tous validés à l'enregistrement (400 hors bornes).

### Modifié

- Étape scenes plus rapide, résultat identique : profil sur un extrait réel de 25 min de la VOD ARC
  (10 fenêtres de parole, 284 images clés) = détection 29,4 s (décodage ffmpeg, plancher), extraction
  des images clés 23,5 s, soit 45 % du temps. Le score de contenu (`ContentDetector`) se calcule en
  un `absdiff` + `sumElems` sur les trois canaux HSV au lieu de trois passes numpy int32 (mêmes
  entiers, mêmes opérations, scores identiques flottant pour flottant), la lecture du tube ffmpeg se
  fait par blocs de 32 images, et les images clés sortent par lots (`[scenes] extract_batch`, défaut 8,
  un seul processus ffmpeg et une seule ouverture du fichier par lot, 1 = un processus par image) avec
  `[scenes] extract_threads` (défaut 2, 0 = défaut ffmpeg) fils de décodage par processus au lieu de
  tous les coeurs disputés par `extract_parallel` processus. Mesure avant/après (extrait 25 min,
  PC à vide, 2 passes) : 53,0 s -> 43,9 s (-17 %), extraction 23,5 s -> 16,5 s (-30 %), détection
  29,4 s -> 27,3 s (-7 %) ; `scenes.json` identique octet pour octet et les 284 jpg identiques.
  Extrapolation à la VOD ARC de 294 min (6093 images clés) : extraction ~8,4 min -> ~5,9 min, soit
  environ 19 min -> ~16 min pour l'étape ; le reste est le décodage ffmpeg, déjà au plancher.
- Téléchargement plus rapide : yt-dlp télécharge les fragments HLS en parallèle (réglage
  `[download] concurrent_fragments`, entier >= 1, défaut 8, refus explicite sinon) au lieu d'un par un
  (constat du 08/10 : VOD Twitch de 11-21 Go à 13-20 Mo/s, plus de 14 min). Le nombre de fragments simultanés
  est journalisé au début du téléchargement. Format, merge mp4, remux fMP4 et reprises réseau inchangés.
- Étape scenes plus rapide : les fenêtres de détection sont analysées par au plus `detect_parallel` processus ffmpeg (défaut 4) ; une fenêtre de plus de `detect_chunk_seconds` (défaut 600 s) est découpée en morceaux contigus détectés en parallèle puis recollés (la scène à cheval sur une jointure est fusionnée, aucune coupure inventée). `analysis_skip_loop_filter` (défaut vrai) ajoute `-skip_loop_filter all` à l'entrée de la détection seulement, jamais à l'extraction des images clés. Un ffmpeg en échec fait échouer l'étape (`ScenesError` nommant la fenêtre) et arrête les autres ; le journal donne fenêtres, parallélisme, durée de détection et d'extraction.
- Veille : un relevé rejoué le même jour (Rafraîchir ou relevé quotidien repris) efface toute la liste des
  propositions du jour, décidées comprises, puis la remplace par les choix de Claude de ce relevé ; « Déjà
  décidées » ne montre plus que les décisions du relevé courant (SPEC-8a45, complète SPEC-bdd9). Conservés :
  `seen.json` (les VOD déjà décidées restent exclues), `history/`, `selection/`, `bilan.json`, la file et les
  vidéos. À la décision, `seen.json` garde aussi source, titre, jeu et chaîne de la VOD, lus par le bilan
  des VOD même si le jour est rejoué.
- Veille : le test d'accès des VOD Twitch se fait par jeu, avec `twitch_access_attempts` essais par VOD, et
  s'arrête dès `max_vods_per_game` VOD accessibles. Les VOD réservées aux abonnés, injoignables, non testées (jeu
  déjà servi) ou non testées à l'échéance sont **écartées et comptées par raison** (bandeau des sources et KPI) :
  plus de « Accès non vérifié ». Le réglage `twitch_access_check_max` est retiré (ignoré s'il reste dans
  `config.toml`).
- Veille : le relevé tourne dans un fil d'arrière-plan du worker qui ne bloque plus la boucle, par voies
  parallèles (une par hôte) en trois phases, sous une échéance globale `veille_deadline_s` (480 s). À l'échéance, les
  sources coupées sont « incomplètes » en orange avec leur message, un bandeau « Relevé incomplet » s'affiche et
  Claude choisit avec ce qui est relevé.
- Veille : `igdb_min_hypes` vaut 5 par défaut et doit être >= 1 ; `igdb_releases_max` est ignoré s'il
  reste dans `config.toml`.

### Corrigé

- TikTok : `[tiktok] content_check = "wait"` ne lance plus de scan dans le vide. L'interrupteur « Vérification de contenu simple » éteint est allumé une fois pour cette vidéo avant d'attendre le résultat (log « interrupteur allumé pour cette vidéo »), au lieu d'attendre 900 s pour rien. Interrupteur déjà allumé : aucun clic. Grisé alors qu'éteint, introuvable ou refusé : arrêt R4 immédiat, sans attente ni publication. `off` inchangé (TASK-0c44).
- Fiche clip lisible : les libellés ne se coupent plus lettre par lettre, le contrôle qualité affiche le type, la sévérité et le détail de chaque problème (au lieu de « [object Object] »), les créneaux et publications sont à l'heure de Paris au format court français, et le compte s'affiche par son nom avec son id en secondaire (« inconnu » s'il n'existe plus).
- TikTok : deux fenêtres superposées (bulle « Nouvelles fonctionnalités » sous « Activer les vérifications automatiques ») : `close_popups` ferme une fenêtre à la fois, la plus haute d'abord (dernière dans le DOM), puis relit les fenêtres visibles ; le clic sur la bulle du dessous était intercepté par l'overlay du dessus. Un arrêt R4 avec capture enregistre aussi le HTML de la page à côté (`.html`) ; un échec d'écriture du HTML est journalisé sans masquer l'arrêt d'origine. Jamais « Activer », jamais de clic de repli (TASK-0bc5).
- Reframe : la réponse « aucune webcam » de Claude n'est plus refusée quand plusieurs visages stables viennent du jeu (menus, ARC Raiders sur PS5) : seul un unique candidat stable ET persistant sur les périodes de la vidéo la contredit ; les visages non persistants sont écartés (warning avec les candidats, raison écrite dans la période), plusieurs persistants restent une erreur explicite (TASK-5979).
- Format stream : le contrôle webcam par clip (webcam repérée sur le seul visage) cherche le visage dans le recadrage agrandi du rectangle, plus sur l'image entière (TASK-0cb1). Mesure réelle v2894178473 (AION 2) : visage trouvé sur 2 images clés sur 41 en image entière (mediapipe courte portée, visage de 60-80 px sur 1920x1080), 39 sur 41 dans le recadrage ; 23 clips sur 24 passaient à tort en letterbox. Réglages `[reframe]` : `facecam_clip_face_margin` (0,25 : marge par côté, fraction du rectangle), `facecam_clip_face_crop_height` (720 : hauteur d'agrandissement en px) et `facecam_clip_face_min_inside` (0,9 : part de la boîte du visage qui doit tomber dans le rectangle ; une webcam déplacée en gardait 0,64 à 0,73, la bonne 1,0). Les 7 clips du constat de TASK-9957 (v2894232594 00-02, v2894088024 03/04/06/09) restent en letterbox, vérifié sur leurs images clés réelles.
- Mineurs de la revue du 08/10 (TASK-2d9a) : (1) un relevé TikTok lancé depuis l'écran et fini après un tour du worker n'est plus ignoré par l'apprentissage jusqu'au relevé suivant : le rattachement et le versement suivent les fichiers de relevé déjà traités (`snapshots` dans `links.json` et `sync.json`), plus la comparaison de `fetched_at` (pris au début du relevé) avec le dernier passage ; (2) `llm.model` de l'état du jour de la veille est le modèle réellement utilisé (`clipper.llm.model_for`), plus l'alias de la config ou `null` ; (3) le rejeu des prompts du coach utilise le modèle du juge concerné, comme le jugement réel ; (4) un appel LLM de la veille ou du coach fait par le worker n'est plus compté dans `llm_usage.jsonl` de la vidéo en reprise : chacun écrit dans son propre journal (`state/veille/llm_usage.jsonl`, `state/learning/llm_usage.jsonl`) ; (5) `review.json` mis de côté est horodaté en UTC explicite (suffixe `Z`), plus l'heure locale du PC.
- Veille : le test d'accès des VOD Twitch finit avant l'échéance. Mesure (état du jour du 08/10, relevé de 11:59 Paris) : 61 VOD sur 191 testées (10 réservées, 5 injoignables, le reste accessible) en moins de 480 s, soit au plus ~7,9 s par VOD, pauses de 3 s des réessais comprises (mesure réelle de yt-dlp non refaite hors CI). Les jeux sont maintenant testés en parallèle borné (`[veille] twitch_access_workers`, défaut 4, de 1 à 16), dans l'ordre de `games` (celui dont Claude se sert : les plus utiles d'abord) ; chaque jeu reste séquentiel (une VOD à la fois, mêmes essais et mêmes pauses sur 10054, aucune requête de plus). `veille_deadline_s` reste la garde : elle coupe les derniers jeux de la liste, leurs VOD restent « non testées » (comptées `deadline`), jamais présumées accessibles. Résultat identique au test séquentiel.
- Veille : le bilan des choix passés transmis à Claude compte les vrais clips produits. Il ne disait « aucun clip »
  pour des VOD AION 2 qui en avaient donné des dizaines : le bilan n'était recalculé qu'après un nouveau relevé TikTok
  (donc figé avant que les clips existent) et n'exposait que les clips publiés. Il est recalculé à chaque passage du
  worker et distingue VOD en traitement (encore dans la file), clips produits, clips publiés et vues à maturité ;
  une donnée manquante est dite inconnue, jamais comptée 0.
- Localisation webcam : Claude voit vraiment le contenu de chaque candidat (constat 08/10, pilote v2894103366 re-rendu : webcam de 90x65 px sur la planche, chiffres « 6 » et « 5 » dessinés dessus, réponse « aucune webcam » et 17 clips sur 18 en letterbox alors que le candidat 6 avait un visage sur 23 images clés). Les numéros sont maintenant dans une pastille collée hors du rectangle ; une seconde image (`facecam/period_N_zoom.jpg`) agrandit chaque candidat, une ligne par numéro, sur `facecam_zoom_frames` images (défaut 3, `facecam_zoom_tile_height` 240 px) ; la liste texte dit « visage détecté sur N image(s) clé(s) ». Garde-fou local symétrique : une réponse « aucune webcam » alors qu'un unique candidat a support et visage sur au moins `facecam_face_stable_share` du plus grand support est remplacée par ce candidat (avertissement, `override` avec `from: null` dans `facecam.json`) ; plusieurs candidats stables : erreur explicite ; aucun : « aucune webcam » conservée.
- Localisation webcam : un cadre sans aucun visage (bannière de sponsor animée, constat 08/10 pilote v2894103366 : cadre 212x150 choisi à la place de la vraie webcam, ~11 clips rendus avec la bannière) n'est plus retenu quand un candidat visage stable existe. Chaque candidat cadre porte maintenant le nombre d'images clés avec un visage dedans (`face_support`, transmis à Claude dans la liste des rectangles : « aucun visage vu dedans »), et un choix de cadre à zéro visage est remplacé localement par l'unique candidat visage vu sur au moins `[reframe] facecam_face_stable_share` (défaut 0,8) du plus grand support, avec un avertissement et `override` dans `facecam.json` ; plusieurs candidats visage stables : erreur explicite. Sans candidat visage stable, une webcam sans visage reste possible (règle SPEC-8257/76dc inchangée). Aucune règle par streamer ou jeu.
- Stream split : un clip dont le panneau webcam ne montre pas la webcam (scène sans caméra, écran de pause où la caméra a bougé) n'est plus rendu en split mais en letterbox. Quand la webcam a été localisée sur le seul visage (aucun bord réel à retrouver), le contrôle par clip exige désormais un visage dans le rectangle sur au moins `[reframe] facecam_clip_face_min_share` (défaut 0,5) des images clés, raison journalisée sinon ; avant, un jeu qui bouge passait pour une webcam vivante (constat 08/10 : 7 clips, mesure réelle 0 % de visage contre 64 à 100 % sur les clips sains). La QA d'un clip `stream_split` demande en plus le défaut bloquant `empty_webcam` (panneau webcam sans visage ni webcam sur la majorité des images) ; les autres formats ne changent pas.
- Écritures JSON atomiques : `scenes.json`, `audio.json`, `meta.json`, `thumbnail.json` et le fichier des
  poids du jury passent par un fichier temporaire puis un remplacement (avec les réessais Windows
  existants) ; un arrêt en pleine écriture ne laisse plus un JSON tronqué que l'étape prendrait pour « déjà
  faite ». Un de ces fichiers déjà tronqué donne une erreur explicite qui nomme le fichier et la commande pour
  refaire l'étape (`--force`), sans réparation silencieuse.
- TikTok : la fenêtre « Activer les vérifications automatiques du contenu ? » est réellement fermée avant le nouvel essai de clic (constat du 08/10 sur TwitchClipperTV : arrêt R4, compte décoché). Cause mesurée dans un vrai Chrome : cette fenêtre est un `TUXModal-overlay` sans `role=dialog` ni `aria-modal`, donc le sélecteur `[modal] container` ne la voyait pas et `close_popups` rendait sans rien fermer ; le conteneur reconnaît maintenant `.TUXModal-overlay`. Le clic du bouton de la fenêtre porte aussi `click_timeout_s` (il n'avait que les 30 s de Playwright). Avec `content_check = "wait"`, la fermeture par « Annuler » est signalée en avertissement : elle refuse seulement l'activation automatique proposée par TikTok, ne désactive pas la vérification de contenu demandée (contrôlée ensuite par le parcours) ; jamais « Activer ». Fenêtre inconnue ou second échec : toujours un arrêt R4, jamais de clic de repli.
- Mode review : après une re-découpe (borne ajustée), un moment que `parts` garde alors qu'il avait été rejeté au premier passage n'a aucune décision humaine ; la vidéo ne finit plus en échec (`KeyError`) mais revient en `awaiting_review` avec ces moments à décider (raison lisible), et rien n'est rendu sans décision (ADR-ad2e). `clipper --config X serve` lance désormais son worker enfant avec le même `--config X` (sans `--config`, inchangé).
- Tests : la suite complète ne laisse plus ~19 Go dans le dossier temporaire. Les images clés synthétiques de `tests/test_reframe.py` passent de BMP 1920x1080 bruités pixel par pixel (6 Mo l'une) à des PNG sans perte au niveau de compression maximal, avec un fond bruité par cellules décalées au hasard à chaque image ; mêmes assertions, aucun test retiré. Mesure du dossier basetemp d'un run de `tests/test_reframe.py` : 18,4 Go avant (713 Mo à 1,4 Go par test lourd), 145 Mo après (au plus 11 Mo par test). `pyproject.toml` : `tmp_path_retention_policy = "failed"` et `tmp_path_retention_count = 1` ne gardent que les dossiers des tests en échec.
- VOD à trous : yt-dlp ne saute plus un fragment indisponible (`skip_unavailable_fragments` faux, réglage `[download] fragment_retries`, défaut 20) ; un fragment perdu fait échouer le téléchargement (`DownloadError`) au lieu de produire une vidéo avec un trou de pts. L'audio extrait (`transcribe.extract_audio`, `audio`) suit la ligne de temps du conteneur (`aresample=async=1:first_pts=0`, silence dans un trou) : le temps du transcript et de `audio.json` égale le temps pts de `-ss`/scenes/render, plus de sous-titres d'un autre passage. Sans trou, sortie identique à l'échantillon près (±0,1 s de durée).
- QA d'un clip `stream_split` (revue r-adr 08/10, M1) : contrôlé selon son vrai format (SPEC-76dc). Le prompt ne parle plus d'un texte d'accroche affiché les 2 premières secondes (rien n'est dessiné) : section `## Format` (webcam en haut, jeu en bas, badge éventuel entre les deux) et titre d'écran seulement si `[render] title_enabled`. L'écran noir est mesuré sur chacun des deux panneaux (`webcam_rect`, `video_rect`), validés comme en stream ; rectangle absent ou invalide = erreur explicite, jamais l'image entière. `face_cut` et `subtitle_on_face` restent demandés (la webcam montre le visage). Letterbox, stream et crop inchangés.
- Coach des prompts du jury : le résultat réel d'un clip publié vient de ses vues (`views_percentile` à maturité, entrées
  `stats` portant `video_id`/`moment_id`, comme la calibration) et non plus de qa + décision, qui valait 1,0 pour tout
  clip publié. Un clip publié sans statistique mûre est exclu des cas, jamais un 1,0 par défaut (revue r-veille-stats I4).
- TikTok : une fenêtre connue qui surgit entre la vérification et le clic (ex. « Activer les vérifications automatiques du contenu ? ») n'arrête plus la publication : si le clic est intercepté, les fenêtres connues sont fermées (« Annuler », jamais « Activer ») puis le clic est refait une fois ; une fenêtre inconnue ou un second échec reste un arrêt R4. Nouveau réglage `[tiktok] click_timeout_s` (10 s, au lieu des 30 s de Playwright).
- Veille (revue r-veille-stats 08/10) : une VOD Twitch déjà en file ou vue n'est plus reproposée au relevé suivant
  (l'id que le worker lui donne, `v2893407960`, est comparé en plus de l'id source). Une exception inattendue du choix
  de Claude est écrite dans l'état du jour (journal ERROR, `finished_at`, aucune proposition inventée) au lieu de
  relancer le relevé à chaque tour du worker. Une limite de session (429) pendant le choix passe `llm.status` à
  `retry` avec `retry_at` (`llm_retry_delay_min`, défaut 30 min) : seul le choix est refait, sans nouveau relevé, au
  plus `llm_retry_max` fois (défaut 3), puis l'échec est explicite.
- Écritures d'état robustes sous Windows : `pipeline` remplace ses fichiers via `channel.replace_retrying` (le mécanisme
  de `channel.atomic_write_json`, 20 essais de 50 ms au lieu de 5, erreur d'origine relevée si le verrou persiste,
  fichier temporaire supprimé) ; le `.tmp` porte pid et thread (plus de vol entre worker et API web). La mise de côté
  de `review.json` (moments refait) utilise le même réessai et ne dépend plus d'un test d'existence préalable : un
  lecteur concurrent ne la fait plus échouer ni sauter (échecs sous charge de `test_run_respecte_lordre_des_etapes` et
  `test_forced_moments_sets_aside_the_stale_review_json`).
- Vision : un lot sauvé dans `vision_partial.json` enregistre les chemins d'images qu'il couvre et n'est repris que
  si ce sont les mêmes que ceux du lot recalculé (sinon, moments refait, il décrivait d'autres images) ; avec
  `--force`, le fichier est ignoré et supprimé. Pipeline : quand l'étape moments est refaite en mode review,
  `review.json` (décisions indexées par moment) est renommé avec horodatage (journal INFO) au lieu d'être appliqué
  aux moments renumérotés (revue r-pipeline 08/10, Important 3 et 4).
- Publication : un post réussi est toujours tracé (revue r-publish 08/10). Le sidecar est réécrit avec les mêmes
  réessais sous Windows que la file (`channel.atomic_write_json`) ; `mark_published` écrit d'abord l'état de file
  (preuve que le post est parti), puis le sidecar : un échec d'écriture est journalisé ERROR avec le `post_url` et
  l'entrée reste `published`, jamais « en cours » ni republiable par Réessayer. La pause d'un compte est revérifiée
  après la prise en main, juste avant le publisher : entrée relâchée en attente avec la raison, aucun post.
- Stats TikTok : les évolutions et pourcentages avec séparateur de milliers (« 4,300.0% », « 4 300,0 % », espaces
  fines et insécables comprises) sont lus correctement au lieu de faire échouer le relevé (« valeur illisible
  (tuile views, 7 jours) ») ; « 1,5% » et « 12,5 % » restent des décimales, les formes ambiguës restent refusées.
- Jury : la re-notation après vision lit la grille enregistrée dans `moments.json` (`rubric.path`) et non celle du style (un style changé de grille entre moments et vision ne provoque plus `KeyError: 'action'`) ; grille enregistrée introuvable : erreur explicite, jamais de repli. L'exploration ne repêche plus un candidat éliminé par le seuil `[gate]` (les rejets `min_score` restent repêchables).
- Téléchargement : une VOD Twitch en mp4 fragmenté (fMP4 : 1 `moov` + des dizaines de milliers de `moof`/`mdat`,
  aucun index) est remuxée sans réencodage en mp4 indexé (`ffmpeg -c copy -movflags +faststart`) avant l'écriture
  de `meta.json`. Constat du 07/10 (v2894103366, 11 Go) : ~30 s par `-ss` avant `-i` contre 0,4 s après remux,
  soit ~6 h pour l'étape scenes. Un mp4 déjà indexé n'est jamais touché ; un remux en échec (ffmpeg absent, code
  non nul, sortie vide) lève `DownloadError`, laisse l'original et n'écrit pas `meta.json`. Réglage `ffmpeg_bin`.
- Veille (relevé réel du 07/10 à 16:56) : les propositions encore à décider s'affichent d'abord, dans l'ordre
  de Claude ; les déjà décidées (mises en file, ignorées) passent dans une section repliée « Déjà décidées ».
  Une proposition mise en file montre son état réel, lu en lecture seule par `GET /api/veille` dans
  `state/queue.json` et `workspace/<video_id>/pipeline.json` : en file, en cours, traitée, à relire, annulée,
  échouée, interrompue ou « retirée de la file » ; plus jamais « en file » quand la vidéo n'y est plus.
  `finished_at` du relevé est la vraie heure de fin (la durée se lit), et non plus l'heure de départ.
- Veille : les abonnés Steam (`memberslistxml`) ne plantent plus toute la source sur un HTTP 429 (relevé
  réel du 07/10 : « HTTP 429 », aucun abonné lu). Le collecteur attend (pause, doublée à chaque essai,
  `Retry-After` s'il est plus long, plafonnée par `steam_followers_retry_wait_max_s`), réessaie le même
  appid au plus `steam_followers_retry_max` fois et journalise chaque attente. Essais épuisés : les
  abonnés déjà lus sont gardés, le reste vaut `null` (`counts.rate_limited`) et la source passe en
  statut `partial` visible, les autres sources continuent. Défauts plus prudents :
  `steam_followers_pause_s` 3 s et `steam_followers_lookups_max` 50 (au plus 60).

## [0.5.3] - 2026-10-07

Version autour de la veille (sorties de jeux IGDB, jeu des VOD YouTube) et de la fiabilité
du téléchargement Twitch. Le zip s'appelle `Clipper-portable-0.5.3.zip` ; la mise à jour se
fait en relançant `Installer.bat` depuis ce zip, tes données ne sont pas touchées. Nouveaux
réglages `[veille]` : `upcoming_days`, `release_window_days`, `igdb_min_hypes`,
`igdb_recent_max`, `igdb_upcoming_max`, `igdb_pages_max`, `youtube_game_min_chars` ;
`[download]` : `network_retries`, `network_retry_pause_s`. La veille reste désactivée par défaut.

### Ajouté

- Veille : écran « Sorties de jeux » (IGDB) entre les propositions et les meilleurs clips : sorties
  récentes (badge J+N) et à venir (badge J-N) avec hypes, plateformes et lien ; badge « Sortie J+N »
  sur les propositions et dans « Ce qui monte » ; source « IGDB (sorties) » dans le bandeau.
  Réglages › Veille : `upcoming_days`, `release_window_days`, `igdb_min_hypes`, validés à
  l'enregistrement (400 hors bornes).
- Veille : la collecte IGDB interroge les jeux de toute la fenêtre de sorties, triés par hypes
  décroissants (plus de jeux du jour sans hype qui noient les vrais), au plus `igdb_pages_max` pages
  de 500. Un jeu sous `igdb_min_hypes` est écarté sauf s'il est déjà en tendance (Twitch, Steam). Les
  sorties récentes sont coupées à `igdb_recent_max` (12) et les à venir à `igdb_upcoming_max` (20) ;
  l'ancien réglage `igdb_releases_max` est ignoré s'il traîne dans `config.toml`.
- Veille : une sortie qui arrive sur une plateforme nouvelle alors que le jeu existe déjà ailleurs
  (par exemple Switch 2 après PS4) porte l'étiquette « Portage », transmise à Claude avec la
  proposition.
- Veille : une VOD YouTube sans jeu reçoit son jeu si son titre ou ses tags contiennent, en mot(s)
  entier(s), le nom d'un jeu déjà relevé aujourd'hui (Twitch, Steam, IGDB). Plusieurs jeux : le nom
  le plus long gagne s'il contient les autres, sinon aucun jeu. Aucune devinette, aucun appel à
  Claude pour ce choix. La carte affiche « jeu déduit du titre » et Claude reçoit les signaux de
  tendance du jeu. Nouveau réglage `[veille] youtube_game_min_chars` (5) : les noms plus courts sont
  ignorés.

### Corrigé

- Download : une VOD Twitch réservée aux abonnés (yt-dlp « subscriber-only content ») est un échec
  définitif avec un message lisible, jamais un échec transitoire réessayé, même si la chaîne
  d'erreurs contient aussi une erreur réseau ou un 403 ; une coupure réseau seule reste réessayée.
- Download : une coupure réseau (connexion fermée par l'hôte distant, WinError 10054 sur
  `usher.ttvnw.net`, « Failed to download m3u8 information ») est réessayée aussitôt, jusqu'à
  `[download] network_retries` fois (15) avec `[download] network_retry_pause_s` (5 s) de pause,
  chaque essai journalisé ; essais épuisés, l'erreur d'origine remonte comme avant. Les autres
  erreurs (abonnés, privé, format) échouent au premier essai.

## [0.5.2] - 2026-10-07

Petite version de correction autour de la publication TikTok et de la veille. Le zip
s'appelle `Clipper-portable-0.5.2.zip` ; la mise à jour se fait en relançant
`Installer.bat` depuis ce zip, tes données ne sont pas touchées. Seul nouveau réglage :
`[veille] twitch_access_check_max` ; la veille reste désactivée par défaut.

### Ajouté

- TikTok : une vérification de contenu bloquée (« Vérification en cours » sans résultat après
  `[tiktok] content_check_retrigger_s`, 120 s) est relancée en décochant puis recochant l'interrupteur ;
  l'erreur rouge « Une erreur est survenue » est relancée par « Réessayer ». Au plus
  `content_check_retriggers` relances (3), toujours dans `content_check_timeout_s` ; erreur persistante
  après les relances : arrêt `content_check` avec le message de TikTok.


### Corrigé

- Veille : les VOD Twitch réservées aux abonnés (que Helix annonce pourtant `public`) ne sont plus
  proposées. Avant le choix de Claude, l'accès de chaque VOD candidate est testé par yt-dlp sans
  téléchargement (nouveau réglage `[veille] twitch_access_check_max`, 30, les plus vues d'abord) ;
  une VOD refusée « abonnés seulement » est écartée et comptée dans le détail de la source Twitch
  (« VOD abonnés écartées »). Une autre erreur (réseau, connexion fermée, délai) ou le dépassement du
  plafond garde la VOD, marquée « Accès non vérifié » avec la raison sur sa carte.

## [0.5.1] - 2026-10-06

Petite version de correction autour de la veille (écran Veille). Le zip
s'appelle `Clipper-portable-0.5.1.zip` ; la mise à jour se fait en relançant
`Installer.bat` depuis ce zip, tes données ne sont pas touchées. Aucun nouveau
réglage : la veille reste désactivée par défaut (`[veille] enabled = false`).

### Ajouté

- TikTok : une vérification de contenu bloquée (« Vérification en cours » sans résultat après
  `[tiktok] content_check_retrigger_s`, 120 s) est relancée en décochant puis recochant l'interrupteur ;
  l'erreur rouge « Une erreur est survenue » est relancée par « Réessayer ». Au plus
  `content_check_retriggers` relances (3), toujours dans `content_check_timeout_s` ; erreur persistante
  après les relances : arrêt `content_check` avec le message de TikTok.
- Veille : chaque VOD proposée (Twitch et YouTube) affiche sa miniature, lue dans
  la réponse de l'API, à la place du bloc gris (qui reste si la miniature manque
  ou si la VOD Twitch est encore en cours de traitement).
- Veille : le détail de la source Twitch indique combien de VOD privées ont été
  écartées.


### Corrigé

- Veille : une VOD Twitch privée n'est plus jamais proposée (champ `viewable` de
  l'API Twitch différent de `public`). Limite connue : l'API Twitch renvoie
  `viewable = public` pour une VOD réservée aux abonnés, donc ces VOD ne sont
  pas détectées et peuvent encore être proposées ; elles échouent alors au
  téléchargement (contenu réservé aux abonnés).
- Veille : un jeu présent dans les ventes Steam FR mais absent du top joueurs
  affiche son rang de ventes et sa montée au lieu de « hors Steam ». « Hors
  Steam » ne reste que pour un jeu absent de toutes les sources Steam.

## [0.5.0] - 2026-10-06

Veille des sujets chauds (Twitch, YouTube, Steam), clips courts, heure libre
par clip dans les séries programmées, légendes avec mots-clés de recherche,
recalage de la webcam et statistiques TikTok relues sur la vraie page. Pas de
fichier de notes séparé dans le dépôt pour cette version : cette section du
changelog suffit (elle sert aussi de corps à la release). Le zip s'appelle
`Clipper-portable-0.5.0.zip` ; la mise à jour se fait en relançant
`Installer.bat` depuis ce zip, tes données ne sont pas touchées. La veille est
désactivée par défaut (`[veille] enabled = false`) et les clips courts aussi
(`[moments] short_clips = false`) : sans réglage de ta part, rien ne change.

### Ajouté

- **Série programmée : coche « Heure par clip »** (TASK-fa00): dans « Programmer
  une série », mode Manuel, une coche (décochée par défaut) donne à chaque clip
  sélectionné (chaque partie d'un clip découpé) son propre champ date et heure
  (heure de Paris), pré-rempli au rythme actuel (début + intervalle, ou à la
  suite de la dernière programmation) et modifiable un par un. L'aperçu et la
  création envoient la date de chaque clip (`clip_dates`) ; chaque date est
  validée clip par clip côté Python avec les refus explicites habituels (fenêtre
  maximale, avance minimale, plafonds, créneau déjà pris sur le compte, même
  heure qu'un autre clip de la série) et le refus s'affiche sous le clip
  concerné. Une partie datée avant (ou à la même heure que) la partie
  précédente est refusée. Décochée : comportement inchangé.
- **Veille : top des ventes Steam du pays** (TASK-2784) : nouvelle source
  `steam_fr` (`IStoreTopSellersService/GetWeeklyTopSellers`, sans clé, pays =
  `[veille] region`, langue = `[veille] language`, `steam_sellers_top` = 50
  places relevées). Les noms viennent de la réponse (pas d'appel `appdetails`).
  Les jeux de `day.games` déjà présents (Twitch, Steam mondial) portent
  `steam_sellers_rank`, `steam_sellers_last_week_rank`, `steam_sellers_gain` ou
  `steam_sellers_new` (« nouveau dans le top ventes FR » : semaine dernière
  absente ou 0), fusionnés par clé normalisée sans doublon ; les jeux nouveaux
  ou gagnant au moins `steam_rank_gain_min` places sont ajoutés comme les jeux
  Steam qui montent (`source = "steam_fr"`, plafond `steam_risers_max`), même
  sans Twitch. Claude les reçoit en contexte ; une erreur de cette source est
  nommée sans bloquer les autres. Écran Veille : colonne « Ventes FR » (« #5
  +107 places » / « Nouveau dans le top ventes FR »), libellés de relevé en
  français (« 99 jeux » au lieu de « 99 games »), et « Jeux qui montent » compte
  aussi les gains de places Steam (mondial et FR).
- **Veille : jeux Steam qui montent visibles sans Twitch FR** (TASK-f4e2) :
  `day.games` ajoute les jeux Steam du relevé absents de la liste Twitch qui
  sont « nouveau dans le top » (`last_week_rank` <= 0) ou gagnent au moins
  `[veille] steam_rank_gain_min` (5) places, plafonnés à `steam_risers_max`
  (10), avec `source = "steam"`, `twitch_match = false` et les champs Twitch à
  `null` (rang inconnu = jeu non retenu, rien d'inventé). Ils sont donnés à
  Claude et affichés dans « Ce qui monte » (« Nouveau dans le top Steam » ou
  « +N places », « hors Twitch FR »), même quand Twitch est en erreur.

- **Légendes : mots-clés de recherche en tête** (TASK-b8a0) : la consigne de
  `captions` exige que la première phrase de la légende contienne le nom du jeu
  (écrit officiellement) et le streamer ou la chaîne de la source (`channel` de
  `meta.json`), puis le moment, en restant naturelle. Jeu ou chaîne inconnus :
  la consigne le dit à Claude, rien n'est inventé. Réglage
  `[captions] caption_keywords_first` (défaut `true`) ; à `false`, consigne
  identique à avant.
- **Veille des sujets chauds** (ADR-ca9a, SPEC-bdd9) : chaque jour le worker
  relève ce qui monte sur Twitch (viewers FR par jeu), YouTube (vidéos
  populaires FR) et Steam (joueurs), compare à la moyenne des 7 jours
  précédents et demande à Claude (usage `veille`, un seul appel texte) de
  proposer 2-3 VOD à clipper d'après tes goûts. Nouvel écran **Veille**
  (navigation et barre basse mobile, compteur = propositions à décider) :
  sources avec leur erreur visible, KPI, propositions avec la raison de Claude
  et le choix du style, boutons « Clipper » / « Ignorer », « Rafraîchir »
  (désactivé pendant un relevé), meilleurs clips du jour (les autres sont
  archivés, jamais supprimés, avec « Restaurer »), tableau « ce qui monte »
  (une donnée absente est expliquée, jamais un 0). Écran Clips : filtre
  « Archivés » (les clips archivés sont masqués ailleurs). Routes
  `/api/veille` (lecture et demandes seulement : le serveur web n'appelle ni
  source ni LLM) et `GET /api/clips?archived=1`. Réglages › Veille : goûts,
  nombres, heure, et les clés d'API (`twitch_client_id`,
  `twitch_client_secret`, `youtube_api_key`) en écriture seule : jamais
  renvoyées par `/api/settings` (seulement `<clé>_set`), masquées dans le
  journal. Désactivée par défaut (`[veille] enabled = false`). Voir
  `docs/GUIDE.md`, section « Veille des sujets chauds ».

- **Clips courts** (TASK-4f5e) : interrupteur `[moments] short_clips` (défaut
  `false`, bornes `short_min` = 20 s et `short_max` = 45 s), réglable par style
  ou dans `config.toml`. Actif, il remplace les durées de la grille (clip
  unique et parties de série) et demande à Claude un clip qui démarre sur le
  moment fort. Par vidéo : case « Clips courts » à trois états (valeur du
  style / oui / non) dans *Ajouter une vidéo*, `--short-clips` /
  `--no-short-clips` en ligne de commande, champ optionnel `short_clips` de
  `POST /api/queue` (les anciennes entrées gardent la valeur du style). Le mode
  est écrit dans `moments.json` et visible sur la fiche de la vidéo. Inactif :
  comportement inchangé.

### Modifié

- **Webcam du stream : bords recalés sur la vraie incrustation** (TASK-893d) :
  une fois le rectangle choisi par Claude (candidats numérotés, SPEC-4a9b), les
  quatre bords sont recalés sur le vrai bord de l'incrustation (vote des pics de
  gradient sur les images de la période, dans une marge bornée réglable
  `facecam_refine_*`, hors du visage stable). La webcam n'est plus décalée et la
  bande de texte de l'overlay ne reste plus visible sous elle. Un côté sans bord
  fiable reste tel quel et la raison est journalisée (jamais de repli
  silencieux). `facecam.json` garde `candidate_rect` (choix de Claude),
  `refined_rect` et `refine_reason`.
- **Dépôt GitHub renommé en TiktokClipper** : les liens de la documentation et
  du changelog pointent vers le nouveau nom.


### Corrigé

- **Statistiques TikTok : le relevé ne s'arrête plus sur `viewers_card`**
  (TASK-429d) : les repères des onglets Spectateurs et Engagement n'avaient
  jamais été confirmés sur la vraie page et le relevé s'arrêtait avec « élément
  attendu absent après 30 s : viewers_card ». Les repères viennent maintenant
  d'un relevé réel en lecture seule (cartes `AnalyticsCard_CardWrapper`) ; les
  barres et les valeurs « <1 % » sont lues, la courbe de rétention (un canvas)
  reste `null`, une valeur introuvable reste `null` ou arrête explicitement le
  relevé, jamais devinée.
- **Veille Steam : noms des jeux sans `GetAppList`** (TASK-7486) : Valve a
  retiré `ISteamApps/GetAppList/v2` (HTTP 404), la source Steam tombait en
  erreur. Les noms viennent maintenant de l'API magasin sans clé
  (`store.steampowered.com/api/appdetails`), demandés seulement pour les jeux du
  top, mis en cache sous `state/veille/steam_names.json` (un nom connu ne se
  redemande pas) et plafonnés par `[veille] steam_name_lookups_max` (100) par
  relevé. Un jeu dont le nom reste introuvable n'est pas relevé et sa raison est
  gardée (`unnamed` dans l'état de la source), jamais un nom inventé.
- **Veille Steam : montée immédiate par le rang** (TASK-7486) : le relevé garde
  `rank` et `last_week_rank` de `GetMostPlayedGames` ; chaque jeu porte
  `steam_rank_gain` (gain de rang vs semaine dernière) et `steam_new_in_top`
  (`last_week_rank` 0 = nouveau dans le top, sans gain chiffré ; champ absent =
  `null`), transmis à Claude avec les candidats à côté de la montée vs 7 jours,
  qui reste `null` tant que l'historique manque.


## [0.4.2] - 2026-10-06

Webcam du stream retrouvée avec l'aide de Claude, garde-fou réseau pour le
navigateur piloté, publication TikTok plus robuste, statistiques plafonnées et
gestion de l'espace disque. Pas de fichier de notes séparé dans le dépôt pour
cette version : cette section du changelog suffit. Le zip s'appelle
`Clipper-portable-0.4.2.zip` ; la mise à jour se fait en relançant
`Installer.bat` depuis ce zip, tes données ne sont pas touchées.

### Ajouté

**Webcam du stream**

- La webcam du stream est maintenant trouvée **par période** (Just Chatting,
  puis jeu) au lieu d'un seul rectangle pour toute la vidéo. Les rectangles
  candidats (visages, cadres nets en mouvement) sont numérotés sur une planche
  et Claude répond seulement par un numéro ou « aucune » : jamais de
  coordonnées inventées. Le choix de chaque clip suit la période où il
  commence, et la décision est écrite par clip pour pouvoir la relire
  (SPEC-4a9b, proposée).

**Réseau**

- La console affiche le **pays de l'IP publique** dans une pastille en haut à
  droite, avec un réglage du pays attendu (`[network]` : `expected_country`,
  `geo_url`, `cache_s`, `block_browser`).
- Le navigateur piloté (TikTok, YouTube) **refuse de s'ouvrir** quand l'IP est
  hors du pays attendu, par exemple VPN actif.
- Au clic sur **Publier** ou **Valider**, une fenêtre d'alerte prévient quand
  l'IP est hors du pays attendu.

**Vidéos et disque**

- Purge des fichiers lourds d'une vidéo, ou de toutes les vidéos terminées,
  avec la taille affichée et une confirmation avant suppression.
- Une vidéo **interrompue** (serveur ou PC arrêté en plein traitement) est
  affichée comme interrompue, jamais comme « en cours » : tu peux la
  **reprendre** là où elle s'est arrêtée ou l'**annuler** (étapes finies
  conservées, action journalisée).
- La miniature des VOD non YouTube (Twitch...) apparaît dès l'ajout à la file.

**Statistiques TikTok**

- Les vidéos **restreintes** (non éligibles à la recommandation « Pour toi »)
  sont repérées au relevé détaillé : pastille par post, résumé « N vidéos
  restreintes sur M en ligne » par compte, texte de TikTok dans la fiche.

**Agencement**

- Éditeur d'agencement split : case **« Afficher le badge »** pour retirer ou
  remettre le badge de style sans perdre sa position enregistrée.

### Modifié

- Statistiques TikTok : le détail est **plafonné aux 30 posts récents**, la
  période par défaut passe à 365 jours, et le relevé lit correctement les
  nombres du type « 1,432 » et la tuile « 24 -2 ».
- Un clip **refusé par TikTok** à la vérification de contenu prend le statut
  `refused_by_platform` : le compte et la série continuent au lieu de
  s'arrêter.
- Les accroches et titres d'écran visent une cible de mots **plus courte**
  que la limite stricte, pour ne plus déborder.
- Quand le pays de l'IP est inconnu (service de géolocalisation injoignable),
  la publication est mise **en attente et réessayée**, le compte reste prêt ;
  si le pays est différent, le worker s'arrête et décoche le compte.
- Les dates des posts TikTok, dont l'année, sont lues en heure de Paris.
- Le contrôle de la webcam par clip a été remplacé par le choix par période
  (voir Ajouté) : une webcam avec visage et cadre n'est plus évincée.


### Corrigé

- TikTok : la vérification de contenu est lue sur le texte **visible** ; un
  « Vérification en cours » resté caché ne bloque plus jusqu'au délai.
- TikTok : une vérification encore en cours n'est plus prise pour finie quand
  « Aucun problème constaté » est affiché ailleurs (fausse fin, nouvel essai,
  fenêtre « Continuer à publier ? » puis échec de publication).
- TikTok : quand la limite quotidienne de vérification est atteinte,
  l'interrupteur grisé ne bloque plus la publication, elle continue sans lui.
- TikTok : le relevé de la liste des Publications est **complet** (liste
  virtualisée, défilement du conteneur, lignes cumulées) au lieu de s'arrêter
  à quelques vidéos.
- Sous-titres : un mot trop long est raccourci (plus de 3 lettres répétées),
  puis coupé net au besoin, au lieu de faire échouer l'étape.
- Worker : le style choisi pour une vidéo est écrit dans `pipeline.json` au
  lancement, donc conservé à la relance ou à la reprise.
- Reframe : la coupe de la webcam est journalisée et le calcul en niveaux de
  gris reste en `uint8`.
- Reframe : une vraie webcam encadrée n'est plus rejetée au contrôle par clip
  quand son cadre réel est décalé de quelques pixels du rectangle retenu
  (tolérance `facecam_clip_edge_tolerance`) ; avant, tous les clips d'un
  stream pouvaient partir en letterbox.
- Console : marges de la barre « Espace disque / Purger » de l'écran Vidéos.
- Tests : le journal global n'écrit plus dans le vrai dossier `logs/` et aucun
  test n'appelle le vrai service de géolocalisation (les tests du navigateur
  piloté échouaient sur une IP hors de France).

## [0.4.1] - 2026-10-04

Installeur portable Windows et calendrier de publication Jour / Semaine /
Mois : le zip `Clipper-portable-0.4.1.zip`, à télécharger depuis la
Release GitHub, installe tout sans outil de développement, et le calendrier de
publication affiche chaque jour sans rien cacher. Pas de fichier de notes
séparé pour cette version : cette section du changelog suffit.

### Ajouté

**Installeur portable Windows** (ADR-e1da et SPEC-38f7, ratifiées)

- Un zip d'amorçage léger, `Clipper-portable-0.4.1.zip` (< 150 Mo : `uv.exe`,
  la wheel et les scripts, jamais de Python ni de site-packages dedans),
  construit par `tools/build_portable.py` et à télécharger depuis la Release
  GitHub. Le programme se construit chez toi à l'installation.
- `Installer.bat` installe Python 3.11, l'environnement, ffmpeg et `claude`
  dans un dossier programme jetable (`%LOCALAPPDATA%\Clipper\app` par défaut),
  prépare un dossier de données séparé (`Documents\Clipper` par défaut,
  jamais touché par une mise à jour), précharge les modèles et termine par un
  `clipper doctor`. Un raccourci et un lanceur ouvrent la console.
- `Desinstaller.bat` supprime le dossier programme et conserve les données
  par défaut (`--donnees` pour tout supprimer) ; il refuse de supprimer si la
  console tourne encore.
- GPU automatique : CUDA seulement si `nvidia-smi` répond, CPU sinon. Pour
  les installations hors installeur, l'extra `clipper[cuda]` installe les
  paquets `nvidia-cublas-cu12` et `nvidia-cudnn-cu12`, et leurs dossiers `bin`
  sont ajoutés au PATH du processus sans manipulation manuelle.
- ffmpeg figé sur une version précise (GyanD 9.0.2, somme de contrôle
  vérifiée) au lieu d'une URL « latest » qui changeait en quelques jours et
  cassait l'installation.
- `clipper doctor` vérifie l'installation et `clipper models prefetch`
  télécharge les modèles à l'avance ; chaque échec est affiché, jamais remplacé en silence.
- Mise à jour par simple relance d'un zip plus récent (refus explicite d'une
  version plus ancienne). Parcours complet dans
  [`docs/INSTALLATION.md`](docs/INSTALLATION.md), nouvelle section
  « Installation sans outils de développement » dans le README, critère n°1 de
  la v1.0.0 reformulé autour du zip portable.
- Test réel optionnel de l'installeur (`CLIPPER_INSTALLER_REAL=1`) : construit
  le vrai zip, l'installe en CPU dans un dossier temporaire, vérifie
  `clipper doctor`, une mise à jour puis la désinstallation complète ; jamais
  lancé en CI ni par défaut. Option `--sans-raccourci` pour ne jamais toucher
  au vrai Bureau.

**Calendrier de publication**

- Trois vues, **Jour**, **Semaine** (par défaut) et **Mois**, avec navigation
  précédent / suivant / aujourd'hui adaptée ; `/api/publish` accepte
  `range=day|week|month` (422 si invalide) et regroupe chaque publication
  dans son jour, calculé en heure de Paris.
- En Semaine, **toutes** les publications du jour sont affichées : plus de
  « +N autres », ni défilement ni coupure, la case s'agrandit (paliers
  normal / compact / mini conservés).
- En Mois, chaque case montre le numéro du jour et le **nombre de vidéos** du
  jour ; un clic sur la case ouvre la vue Jour.
- Les créneaux libres restent des cibles de glisser-déposer en Jour et en
  Semaine, jamais tronqués.
- La **légende en couleurs** (planifié / programmé, publié, échec) reprend
  les couleurs des boîtes du calendrier.

### Modifié

- Semaine : la colonne vide de gauche (reste de l'ancienne grille horaire)
  est retirée, sept colonnes de jours.
- Le critère n°1 de la v1.0.0 (`docs/versions.md`) porte sur le zip portable
  de la Release.


### Corrigé

- Séries programmées : quand des clips validés existent mais qu'aucune série
  ne tient dans N places, le message n'affirme plus à tort « aucun clip
  validé » : il indique combien de clips et de parties sont validés et
  propose de passer à N vidéos ou de décocher « Parties ensemble ».
- Installeur, 16 points confirmés par une relecture : `uv.exe` et
  `clipper.exe` appelés par leur chemin complet, relance possible après une
  installation interrompue, override OpenCV livré dans le zip (un seul paquet
  OpenCV installé), lanceur en encodage OEM, pause sur erreur en
  double-clic, `install.json` relu à la relance, désinstallation qui vérifie
  le dossier programme, `Desinstaller.bat` et icône copiés dans `app`.
- Installeur, corrigé grâce au vrai passage de test : `Installer.bat` et
  `Desinstaller.bat` pointaient le mauvais chemin de `install.ps1` ;
  `uv pip install` sans `--python` pouvait installer dans un environnement
  ambiant (désormais explicite) ; `app\version.txt` n'était jamais écrit, si
  bien qu'une relance se croyait en première installation ; le script
  `claude.ai/install.ps1` peut renvoyer un `Byte[]` (plantait avec
  `Invoke-Expression`) et `claude` installé restait hors du PATH du processus
  courant.
- Tests dépendants de l'heure rendus déterministes : plusieurs tests
  (plafond par jour TikTok/YouTube du worker, semaine publiée) lisaient
  l'horloge deux fois et pouvaient échouer près de minuit ; l'horloge est
  figée, le calcul de « lundi » se fait dans le fuseau du compte (Paris) et
  non plus en UTC. Aucun bug du code livré n'a été révélé.

## [0.4.0] - 2026-10-03

YouTube Shorts et publication avancée : publication immédiate ou programmée
sur YouTube (comme TikTok), séries programmées, approbation groupée, calendrier
de publication toujours visible, styles détachés du compte de publication,
éditeur d'agencement letterbox, journal global de toutes les actions, et une
série de correctifs de fiabilité (revues de code sur la publication, les
comptes, le worker et la transcription). Pas de fichier de notes séparé pour
cette version : cette section du changelog suffit.

### Ajouté

**YouTube**

- Comptes YouTube (service `youtube` à côté de `tiktok`), connexion par un
  Chrome normal sur `studio.youtube.com`, « prêt à publier » vérifié comme
  pour TikTok (nom de chaîne et identifiant de chaîne enregistrés).
- Publication sur YouTube Shorts, immédiate ou programmée, par pilotage de
  YouTube Studio (`clipper/youtube.py`) : titre, description, visibilité,
  case « conçue pour les enfants », `#Shorts` ajouté si absent, lien
  `youtube.com/shorts/<id>` enregistré ; plafond de publications par jour et
  écart minimal entre deux publications (SPEC-5e50) ; toutes les dates
  saisies (TikTok et YouTube) en heure de Paris, jamais celle du PC.

**Publication et séries**

- « Programmer une série » : choisis un compte, un style (optionnel), un
  nombre de vidéos et un intervalle en heures ; Clipper choisit les N
  meilleurs clips validés (score décroissant, parties d'un clip ensemble et
  dans l'ordre), calcule les dates (passage heure d'été/hiver géré), affiche
  un aperçu, puis crée les publications d'un coup (SPEC-1ed3).
- Le mode automatique d'une série (programmée ou non) ne pioche plus que
  dans les clips déjà validés (approuvés) du compte choisi ; le nombre de
  vidéos demandé est plafonné au nombre de clips réellement disponibles, et
  une coche « Parties ensemble » (activée par défaut) impose qu'un clip
  découpé en plusieurs parties soit toujours programmé ou publié en entier.
- « Programmer une série » : coche « À la suite de la dernière
  programmation » (décochée par défaut) ; la série démarre à la dernière
  publication à venir du compte + l'intervalle, date calculée côté serveur
  et affichée à l'heure de Paris.

**Approbation**

- Écran Clips : sélection multiple et approbation groupée pour un même
  compte (« Sélectionner », cases à cocher, barre d'action en bas) ; cocher
  une partie d'un clip découpé sélectionne toute sa série ; tout ou rien
  (un clip déjà publié ou refusé dans la sélection annule tout).

**Calendrier**

- Écran Publication : le calendrier hebdomadaire est désormais toujours
  affiché (passé et futur, publications Clipper et déclarées manuellement),
  même sans compte choisi ni créneaux réguliers ; la liste des clips validés
  à publier n'apparaît plus deux fois (fusion en une seule liste « En
  attente ») ; troisième façon de dater une publication : « Après la
  dernière programmation + N h ».

**Styles et agencement**

- Éditeur d'agencement visuel pour le format letterbox (classique), sur le
  modèle de celui du format stream `split` : zones réglables par
  glisser/redimensionner ou valeurs numériques (vidéo nette, titre d'écran,
  bande des sous-titres, pseudo de chaîne), avec retour au standard du
  dépôt par un bouton.

**Journal**

- Journal global de toutes les actions, tous processus confondus (`serve`,
  `worker`, `run`, CLI) : un fichier par jour sous `logs/` (horodatage
  Europe/Paris), purgé automatiquement au-delà de 2 jours, écran « Journal »
  dans la console (menu Configuration). Chaque requête web et chaque action
  qui modifie des données (comptes, publications, styles...) y est
  consignée, mots de passe et jetons masqués.

**Moments**

- Grille de notation (`rubric.toml`) : plancher `min_moments_cap` (3 par
  défaut) sous le plafond horaire, pour qu'une courte vidéo avec plusieurs
  bons moments n'en garde pas qu'un seul (SPEC-4063 règle 4).

**Transcription**

- Chaque tranche de correction réussie est mise en cache séparément : un
  nouveau passage de l'étape `transcribe` ne redemande que les tranches
  manquantes au lieu de tout refaire si une seule bloque ; délai par tranche
  réglable (`[transcribe] fix_timeout_s`) ; une tranche en échec transitoire
  est relancée une fois seule avant de faire échouer toute l'étape.

### Modifié

**Styles et agencement**

- Un style (preset de chaîne) n'a plus de compte de publication ni de
  créneaux associés : le compte se choisit par publication, et les créneaux
  réguliers se règlent désormais sur le compte lui-même, dans l'écran
  Comptes (SPEC-6076 R2). Un preset existant qui avait encore un
  compte et des créneaux est migré automatiquement (créneaux reportés sur
  ce compte s'il n'en a pas déjà, avertissement journalisé une fois).

**Comptes**

- Écran Comptes : créneaux réguliers de publication réglables par compte
  (ajout/suppression), repris par le worker et affichés dans le calendrier
  de Publication.

**Worker et fiabilité**

- Un seul compte piloté à la fois, tous services confondus (TikTok et
  YouTube), par un verrou de pilotage inter-processus ; la prise en main
  d'une publication programmée est atomique (vérifiée sous verrou avant de
  piloter le navigateur) ; une série dont les parties sont liées n'avance
  plus à la partie N tant que la partie N-1 n'est pas publiée.
- L'annulation d'une vidéo depuis la console passe par la file de traitement
  (le serveur web ne crée plus de `Worker` lui-même) : plus d'erreur 500 à
  l'annulation, et aucune publication en cours n'est mise en échec par un
  arrêt du serveur.


### Corrigé

- **YouTube** : l'envoi d'une vidéo s'ouvre désormais par « Créer » ->
  « Importer des vidéos » ; l'ancienne URL directe d'envoi redirigeait
  silencieusement vers le tableau de bord et faisait échouer la publication.
- **Publication, comptes, styles** (revue de code) : attribuer un style à
  une vidéo qui n'en avait pas ne fait plus republier un clip déjà publié ni
  perdre ses publications antérieures ; l'approbation groupée est
  réellement tout ou rien (tous les clips validés avant toute écriture) ;
  suppression d'un style avec des publications en cours refusée (liste des
  publications concernées) ; réglages de publication (visibilité,
  commentaires...) non perdus à l'approbation ; accès concurrent à
  `accounts.json` protégé par un verrou inter-processus ; plafonds
  quotidiens et écart minimal calculés dans le fuseau du compte ; publier un
  clip d'une vidéo sans style accepté dès que le compte est choisi.
- **Worker, pipeline, transcription** (revue de code) : une erreur sur une
  vidéo ou sur un fichier de file d'attente illisible n'arrête plus la
  boucle du worker (journalisée, vidéo suivante traitée) ; le mode choisi
  pour un style est bien transmis au sous-processus de traitement ; les
  décisions de revue humaine sont prises en compte après la génération des
  sous-titres (captions) et non avant ; `retry_delays` vide ne bloque plus
  un re-essai ; une correction identique à la précédente n'est plus comptée
  comme un refus ; une ponctuation déjà présente n'est plus doublée.
- **Publication** : une entrée déjà publiée, en cours de pilotage ou
  programmée ne peut plus être approuvée ni refusée par erreur (y compris
  une partie sœur d'une série) ; une publication programmée ou déjà publiée
  via Clipper n'est plus annulable par erreur ; une partie ne s'approuve que
  si la précédente est publiée ; les créneaux déjà pris par un compte
  comptent pour tous les styles confondus (plus de double réservation d'un
  même instant) ; l'annulation d'une série en échec partiel liste
  désormais les publications déjà prises en charge par le worker au lieu
  d'échouer en silence.
- **Journal** : le handler du journal global n'est plus fermé par la
  reconfiguration du logging d'uvicorn (plus jamais d'erreur 500 une fois le
  serveur monté).
- **Console** : aperçu d'une série relancé automatiquement à chaque saisie
  (le bouton « Valider » ne reste plus grisé à tort, le nombre annoncé reste
  à jour) ; résumé des dates refusées affiché dans l'aperçu d'une série ;
  service (TikTok ou YouTube) affiché dans le choix du compte de l'écran
  Clips.

### Retiré

- Clés `[channel] tiktok_account` et `[channel] slots` d'un preset de style :
  migrées automatiquement vers le compte concerné puis retirées (voir
  « À savoir pour migrer » ci-dessous). Un style sans publication associée
  jusqu'ici continue de fonctionner, sans ces clés.

### À savoir pour migrer

- Un preset de style (`presets/<nom>.toml`) qui avait encore `tiktok_account`
  et `slots` dans sa table `[channel]` est migré tout seul au prochain
  chargement : ses créneaux passent sur ce compte TikTok (sauf si le compte
  a déjà des créneaux, auquel cas il n'est pas écrasé) et les deux clés sont
  retirées du preset à la prochaine sauvegarde. Règle les créneaux de
  publication depuis l'écran Comptes désormais, pas depuis l'écran Styles.
- Nouveau dossier `logs/` (journal global, un fichier par jour) : ignoré par
  git, se purge tout seul au-delà de 2 jours, rien à faire.

## [0.3.0] - 2026-10-03

Pré-version « prête pour l'usage réel » : corrections et réglages issus du
premier usage de la 0.2.0, sans grosse nouveauté. Attention : la clé
`[parts] rubric_path` est retirée. Notes détaillées et marche à suivre pour
migrer : [`docs/releases/v0.3.0.md`](docs/releases/v0.3.0.md).

### Ajouté

- Lanceur `Clipper.bat` à la racine : démarre `clipper serve` s'il ne tourne
  pas déjà puis ouvre `http://127.0.0.1:8000`. `tools/creer-raccourci.ps1`
  crée `Clipper.lnk` avec le logo (`tools/clipper.ico`) ; décrits dans la
  section de lancement du README.
- Console, radar du jury : un moment retenu par le jury mais écarté au
  découpage affiche « retenu par le jury, écarté au découpage : raison ».
- README refait avec captures et animations de la console, et image d'aperçu
  social du dépôt (`docs/assets/social-preview.png`).

### Modifié

- **Statistiques TikTok** (SPEC-47e2, remplace SPEC-86fe) : le relevé n'a lieu
  que lorsque tu te sers de Clipper (ouverture de l'écran Statistiques si le
  dernier relevé date de plus de `[tiktok] stats_stale_min` minutes, 60 par
  défaut ; passage d'une publication ; bouton « Relever maintenant »), avec un
  seul relevé à la fois par compte. Le relevé périodique du worker est coupé
  par défaut (`[tiktok] stats_interval_h = 0`).
- La liste Publications de TikTok Studio est lue en entier (défilement, plus
  de 50 posts, avec une limite de sécurité).
- Un compte neuf sans post est reconnu (liste vide) : plus d'attente de 30 s
  ni de publication arrêtée. Les posts supprimés sur TikTok disparaissent de
  l'affichage ; l'historique des relevés est conservé.
- Console : Publication et Statistiques se filtrent par compte TikTok (« Tous
  les comptes ») au lieu de la chaîne.
- « Chaîne » devient « Style » dans la console et la documentation (les
  identifiants internes ne changent pas : `presets/`, `[channel]`,
  `/api/channels`).
- Console, Publication : les cartes du calendrier tiennent sur deux lignes et
  le titre complet s'affiche en info-bulle.


### Corrigé

- L'étape `parts` découpe avec la grille réellement utilisée par `moments`
  (`rubric.path` de `moments.json`). Avant, une chaîne en grille gaming
  (30 à 90 s) voyait des moments valides rejetés au découpage (grille
  standard, 60 à 120 s) après avoir pris une place du plafond par heure.
- Un post publié via Clipper sur un compte non lié à une chaîne n'apparaissait
  pas dans Publication.
- `clipper.__version__` valait encore `0.1.0` ; il suit désormais la version
  du paquet.

### Retiré

- La clé `[parts] rubric_path` : la grille de découpage est celle de l'étape
  `moments`. Si elle est encore dans `config.toml`, `parts` s'arrête avec une
  erreur claire ; supprime la clé (règle `[moments] rubric_path` à la place).

## [0.2.0] - 2026-10-02

Console web v2, publication et statistiques TikTok par navigateur, comptes
rangés dans le coffre de l'OS, grille gaming et confiance du jury. Notes
détaillées et marche à suivre pour migrer :
[`docs/releases/v0.2.0.md`](docs/releases/v0.2.0.md).

### Ajouté

**Console web**

- Console de gestion web v2 (`python -m clipper serve`, `clipper/web/`) en neuf
  écrans : Tableau de bord, Vidéos, Revue, Clips, Publication, Chaînes,
  Statistiques, Comptes et Réglages. Page statique sans étape de build,
  temps réel par SSE avec repli sur interrogation toutes les 5 s,
  utilisable sur téléphone, polices embarquées.
- Écran Vidéos : liste filtrable, ajout par URL avec choix de la chaîne, fiche
  avec la frise des 12 étapes (durées, progression, journal suivi en direct),
  « relancer depuis cette étape », annulation.
- Écran Revue : lecteur calé sur le moment, timeline aux bornes glissables,
  justification du jury, raccourcis `A` / `R` / `J` / `K` / espace, « Annuler »
  pendant 5 s ; le rendu n'est proposé que lorsque chaque moment a une décision.
- Écran Clips : galerie 9:16, fiche du clip (QA, partie N/M), édition de la
  description, des hashtags et du titre d'écran (avec nouveau rendu),
  approuver / refuser, télécharger, copier.
- Diagramme en étoile du jury par moment dans la fiche vidéo (étape Moments).
- Accès distant par jeton : `serve --host` hors `127.0.0.1` exige `[web] token`
  (refus de démarrer sinon) ; page de saisie du jeton, cookie, 401 sur `/api`
  et `/media`. Réseau local seulement, pas de TLS.
- Notifications du navigateur (permission demandée depuis un réglage local,
  jamais au chargement) sur `done`, `failed`, `awaiting_review`, `queued`.
- Sortie console détaillée : `-v` (progression par étape, une ligne par appel
  LLM, résumé final) et `-vv` (détail) ; relance ciblée d'étapes par
  `--force-step` ; journal `workspace/<video_id>/events.jsonl`.

**Chaînes**

- Une chaîne = un preset `presets/<nom>.toml` fusionné clé par clé sur
  `config.toml` (table `[channel]` : nom, source, surveillance, mode, créneaux,
  fuseau, compte TikTok, logo), utilisable aussi par `--config`. `presets/`
  est ignoré par git.
- Écran Chaînes : création et édition par formulaire (valeurs héritées
  visibles, erreurs de validation sous le champ), éditeur d'agencement visuel
  (zones webcam, jeu, badge et sous-titres sur une image clé), aperçu du style
  des sous-titres, choix de la grille de notation.
- Surveillance des VOD d'une chaîne (`[channel] watch`) : mise en file
  automatique en mode `auto`, VOD « à confirmer » sur le tableau de bord en
  mode `review`.
- Agencement stream `split` (`[reframe] stream_variant = "split"`, SPEC-76dc) :
  webcam en haut, jeu en bas, badge de chaîne optionnel (logo et nom) à leur
  jonction, titre d'écran désactivable (`[render] title_enabled`), style des
  sous-titres réglable (police, couleurs, contour, ombre, position).

**Comptes et coffre**

- Écran Comptes : carnet local des comptes (libellé, plateforme, identifiant,
  notes), boutons Copier, générateur de mot de passe (12 à 64 caractères).
  Les mots de passe ne vont que dans le coffre de l'OS (`keyring`), jamais
  dans un fichier.
- Profil de navigateur par compte (`state/browser/<compte>/`), connexion
  manuelle par `python -m clipper browser login <compte>` ou depuis l'écran
  Comptes, état de connexion TikTok vérifié localement (jamais connecté,
  connecté, session expirée).
- Case « prêt à publier » calculée automatiquement : cochée quand la connexion
  est vérifiée et qu'aucun arrêt (captcha, vérification) n'est en attente.

**Publication TikTok**

- Publication par pilotage d'un vrai Chrome (Playwright, `clipper/tiktok.py`,
  `[tiktok]`) : immédiate ou programmée côté TikTok, avec lien du post
  récupéré, arrêt sûr sur captcha ou page inattendue (capture d'écran, bouton
  Réessayer), plafond de posts par jour et écart minimal par compte, délais
  aléatoires entre actions. Repères de la page dans
  `clipper/assets/tiktok_selectors.toml`.
- Écran Publication : formulaire « Nouvelle publication » (clip, compte,
  maintenant ou programmé, légende, visibilité, commentaires, réutilisation,
  étiquette IA), calendrier hebdomadaire des créneaux, statut de chaque
  entrée, « Déclarer publié (hors Clipper) ». Une vidéo sans chaîne reste
  publiable.
- Cookies YouTube lus depuis un profil de navigateur (`[download]
  cookies_profile`) pour les vidéos qui exigent une connexion.
- Test réel optionnel, publication privée sur un compte de test :
  `CLIPPER_TIKTOK_REAL=1 pytest tests/integration/test_tiktok_real.py`.
- Étude de cadence de publication : `docs/tiktok-cadence.md`.

**Statistiques TikTok**

- Écran Statistiques par compte, alimenté par TikTok Studio : vue d'ensemble
  (7 / 28 / 60 jours, évolution, courbe par jour), liste des vidéos triable,
  fiche de statistiques par vidéo, y compris les posts publiés hors Clipper.
- Relevé à la demande et périodique par le worker (`[tiktok] stats_interval_h`),
  historique horodaté sous `state/stats/tiktok/<compte>/` jamais écrasé ; une
  valeur que TikTok n'affiche pas encore reste vide, jamais 0.

**Jury et grille gaming**

- Confiance de chaque juge (0 à 100) par candidat : un débat s'ouvre si une
  confiance passe sous `[jury] debate_confidence_below` (40), la médiane est
  pondérée par la confiance (plancher `min_confidence_weight`, 0,2).
- Grille gaming embarquée : `[moments] rubric_path = "builtin:gaming"`, choisie
  par chaîne (émotion pondérée en tête, clips de 30 à 90 s).

**Worker et file**

- Worker (`python -m clipper worker`, lancé par `serve`) : file
  `state/queue.json`, une vidéo à la fois dans un processus enfant, annulation,
  reprise au redémarrage, voyant « worker actif » dans la console ; il pilote
  aussi la surveillance des VOD, la publication et le relevé des statistiques.
- Dossier `state/` : tout l'état hors vidéo en fichiers JSON sous verrou
  inter-processus (file, surveillance, publication, comptes, statistiques).

**Documentation et outils**

- `docs/GUIDE.md` : section « Console de gestion » ; `docs/versions.md` : plan
  de versions et critères de la 1.0.0 ; `docs/benchmarks/whisper-modeles.md` :
  banc `small` contre `large-v3-turbo`.
- `tools/setup.ps1` vérifie aussi `playwright` et Google Chrome.
- `clipper.gpu.vram_used_mb()` (mesure par `nvidia-smi`) affichée sur le
  tableau de bord.

### Modifié

- Statistiques TikTok : les onglets Spectateurs et Engagement ne sont ouverts qu'à
  partir de 100 vues (`[tiktok] stats_audience_min_views`) ; un relevé détaillé
  passe de 77 s à 13 s pour un post récent.
- **Format stream** (SPEC-8257) : le choix stream ou letterbox d'un clip se fait
  sur la présence de la webcam elle-même (contenu non noir, bords retrouvés,
  non figé) et non plus sur la détection du visage ; la localisation de la
  facecam, une fois par vidéo, garde le visage comme indice avec un seuil plus
  bas (`facecam_localize_min_share`, 0,1).
- **Titre d'écran sobre** (SPEC-6a86) : sans emoji ni superlatif par défaut
  (`[captions] screen_title_allow_emoji`, `screen_title_forbidden_words`) ;
  la légende et l'accroche suivent la même sobriété (`caption_allow_emoji`).
- **Sous-titres** : plus rien à l'écran pendant les silences.
- **Jury** : chaque juge doit renvoyer sa confiance par candidat ; une réponse
  sans confiance est invalide, comme tout champ manquant.
- **Détection de scènes** : seules les plages de parole (marge
  `speech_margin_seconds`) sont décodées ; elle exige désormais la
  transcription faite.
- **Rythme de publication** : défauts d'un compte neuf (`max_posts_per_day = 1`,
  `min_gap_minutes = 480`) ; délais entre actions de 0,3 à 1 s ; vérification
  de contenu de TikTok coupée par défaut (`[tiktok] content_check = "off"`,
  `"wait"` pour l'attendre).
- **Transcription** : modèle `small` conservé par défaut ; `large-v3-turbo`
  (pic VRAM 3,29 Go) reste une option au cas par cas.
- **`--config`** : le fichier passé est désormais un preset fusionné clé par clé
  sur `config.toml` (qui doit exister) au lieu de le remplacer ; un preset de
  la 0.1.0 continue de fonctionner.
- **Configuration** : `config.toml` s'écrit depuis la console, validé avant
  remplacement atomique ; nouvelles sections `[web]`, `[worker]`, `[watch]`,
  `[publish]`, `[channel]`, `[browser]`, `[accounts]`, `[tiktok]`.
- **Dépendances** : `playwright`, `keyring`, `tomli-w`, et `tzdata` sous
  Windows ; `pytest-xdist` pour les tests (`-n 6` par défaut).
- Le dépôt suit le versionnage sémantique (`docs/versions.md`).


### Corrigé

- Sous-titres : un jeton collé par apostrophe ou ponctuation isolée n'est plus
  fusionné avec le mot précédent à travers un vrai silence.
- Titre d'écran : le schéma JSON envoyé au LLM contredisait encore la règle
  « sans emoji ».
- Worker : `--config` placé avant la sous-commande est pris en compte, et un
  processus enfant qui meurt n'efface plus la vidéo en silence.
- Publication TikTok, corrigé après les premiers tests réels : fenêtres
  connues fermées, menus déroulants ne sont plus pris pour des fenêtres,
  vidéo privée (« Maintenant » grisé), cases de réglage cochées par clic,
  suggestions de hashtags refermées, case désactivée par TikTok journalisée
  et laissée, succès prouvé par la page Publications, privé et programmé
  refusés avant d'ouvrir le navigateur.
- Statistiques TikTok : repères réels de la page Publications (lignes des
  posts, vues, likes, commentaires, visibilité, date) et du menu des périodes
  de TikTok Studio ; date « 2 oct., 12:30 » sans année lue avec l'année du
  relevé (un post récent est bien relu en détail).
- Console, plusieurs séries de corrections : miniatures des clips et des
  vidéos, erreurs affichées en clair (plus de `[object Object]`), compteurs,
  voyant du worker, tri des vidéos, cache des fichiers statiques, échecs
  actionnables, marges et libellés.

### Sécurité

- Accès distant refusé sans jeton, 401 sur `/api` et `/media` ; le jeton ne
  se modifie pas depuis l'interface, seulement dans `config.toml`.
- Mots de passe des comptes uniquement dans le coffre de l'OS : erreur
  explicite plutôt que repli vers un fichier si aucun coffre sûr n'est
  disponible ; jamais renvoyés par la liste, ni écrits dans un journal.
- Routes `/api/accounts*` accessibles depuis le PC seulement (adresse de
  bouclage et en-tête `Host` vérifiés, même avec un jeton valide).
- Clipper ne saisit jamais ton identifiant ni ton mot de passe TikTok : la
  connexion se fait à la main dans un Chrome normal ; captcha ou vérification
  = arrêt immédiat, jamais de contournement.
- Seuls les cookies YouTube et Google d'un profil sont exportés vers
  `cookies.txt` ; `state/` et `presets/` sont ignorés par git.

### Retiré

- Aucune commande, route ni réglage de la 0.1.0 n'est retiré.
- Ne s'appliquent plus : « aucune publication automatique sur TikTok » et
  « surveillance d'une chaîne Twitch pas encore couverte » (limites de la
  0.1.0).

## [0.1.0] - 2026-09-30

Première pré-version : pipeline complet YouTube/VOD Twitch -> clips TikTok
verticaux sous-titrés, en local.

### Ajouté

- Pipeline en 12 étapes (`download, transcribe, scenes, audio, moments,
  vision, parts, captions, reframe, subtitles, render, qa`), chaque étape un
  module `clipper/` qui lit/écrit sous `workspace/<video_id>/`, enchaînées
  uniquement par `clipper.pipeline` (ADR-b16b).
- Téléchargement YouTube et VOD Twitch (`twitch.tv/videos/<id>`) via yt-dlp.
- Transcription mot par mot (faster-whisper, `BatchedInferencePipeline`,
  `batch_size=8`), avec correction de la transcription et du vocabulaire de
  noms propres par LLM.
- Sélection des moments forts par LLM notée selon une grille (`rubric.toml`,
  SPEC-0eec), avec un mode jury à cinq juges (retention, spectateur,
  monteur, avocat, conformite) pour la sélection en mode `auto` (ADR-ff87),
  débat déclenché sur écart de score et apprentissage à partir des
  décisions passées sans uniformisation des juges (ADR-1cf0).
- Ajustement visuel des moments par lecture des images clés (étape
  `vision`), découpe des moments trop longs en plusieurs parties (`parts`).
- Génération du titre, de la légende, des hashtags et de l'accroche de
  chaque clip par LLM (`captions`).
- Deux formats de recadrage vertical (`[reframe]`) : `letterbox` (zoom fixe,
  titre d'écran en haut, sous-titres dans la bande floue du bas, SPEC-6127)
  et `stream_auto` (facecam détectée une fois, agrandie en haut, jeu en bas,
  jamais de bascule dans un clip, SPEC-3a88) ; `crop` (suivi de visage)
  conservé comme option figée.
- Sous-titres mot par mot (.ass) avec emphase choisie par LLM.
- Appel à l'abonnement optionnel (SPEC-6a47, désactivé par défaut) : pseudo de
  chaîne discret sous le titre d'écran, carte de fin « Abonne-toi ! » sur les
  dernières secondes, ligne d'appel et hashtags dans la description ; réglé
  par un fichier de config par chaîne (`--config`).
- Rendu ffmpeg (image, sous-titres, titre d'écran, audio normalisé),
  encodeur choisi par `clipper.gpu` (NVENC si CUDA détecté, sinon libx264).
- Contrôle qualité automatique (`qa`) : résolution, durée, silence de tête,
  image noire — un clip n'est prêt que si `qa.is_ready` le confirme.
- Deux modes d'exécution : `review` (validation humaine de chaque moment
  via `python -m clipper decide` ou l'interface web) et `auto` (le
  pipeline va jusqu'au bout, `qa` remplace la revue humaine), sans repli
  silencieux sur erreur (ADR-ad2e) : une erreur transitoire remet la vidéo
  en file d'attente (`python -m clipper queue`), une erreur définitive la
  passe `failed`.
- Interface web locale (`python -m clipper serve`, `clipper/web/`) : page
  statique servie par FastAPI, aucune logique métier côté serveur
  (ADR-09ad).
- Tout appel LLM centralisé dans `clipper.llm.ask(...)` (ADR-b1c1),
  backends interchangeables (`claude-cli` par défaut, `claude-api`,
  `ollama`), modèle configurable par usage, réponse validée contre un
  schéma JSON, consommation journalisée par vidéo
  (`workspace/<video_id>/llm_usage.jsonl`).
- Résolution automatique CPU/CUDA (`clipper.gpu`), un seul modèle lourd en
  VRAM à la fois, libéré explicitement après usage (ADR-fb9b).
- Suite de tests tournant entièrement sur CPU, sans réseau ni vrai
  modèle/Claude par défaut (`FakeBackend`) ; tests optionnels marqués
  `skipif`, sautés sauf variable d'environnement explicite
  (`CLIPPER_CLAUDE_INTEGRATION=1`, `CLIPPER_REAL_MODELS=1`).
- Script d'installation `tools/setup.ps1` (vérifie uv, Python 3.11, ffmpeg,
  `claude`, `ank`, GPU optionnel).

### Connu comme limite

- Pré-version : pas de garantie de stabilité de l'interface en ligne de
  commande ni du format de configuration.
- Chaque vidéo traitée consomme du quota Claude (coût variable selon la
  durée, le nombre de clips et le mode de sélection ; voir
  `docs/benchmarks/rtx3050.md`).
- La grille de notation des moments (`rubric.toml`) est calée sur la
  parole : un moment fort sans dialogue marquant peut être sous-noté.
- Aucune publication automatique sur TikTok : le dépôt produit les clips
  et leurs métadonnées, la mise en ligne reste manuelle.

[Non publié]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.5.3...v0.6.0
[0.5.3]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/ZeDKouill3/TiktokClipper/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ZeDKouill3/TiktokClipper/releases/tag/v0.1.0