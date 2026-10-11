---
id: SPEC-00dbf5ef4082
type: spec
slug: boucle-d-apprentissage-rattachement-post-clip-ap
title: "Boucle d'apprentissage : rattachement post→clip après relevé, versement stats→outcomes avec video_id/clip_id/moment_id, métrique views_percentile à maturité par compte, recalibrage automatique, coach sur déclencheur validé dans l'interface (Adopter/Refuser), bilan des VOD de veille dans le prompt ; réglages [learning], état state/learning/, tests sans réseau"
created: 2026-10-07T10:26:36Z
author: w-learnplan
status: superseded
scope:
  - clipper/jury_calibration.py
  - clipper/jury_coach.py
  - clipper/worker.py
  - clipper/veille.py
  - clipper/publish.py
  - clipper/web/**
references: [ADR-c260c4ada286, ADR-1cf0b17d48b3, ADR-ad2e562b1810, ADR-b1c17749b528, ADR-b16b71007578, ADR-ca9a5792739c, SPEC-47e204a92fd0, SPEC-bdd9e0db8905]
ratified: 4e98ef461ebe
verified:
  - by: nicoc@zedk_ordi
    at: 2026-10-07T10:32:22Z
schema: 4
version: 4
---

## Objet
Règles de la boucle d'apprentissage branchée sur les relevés réels (ADR-c260,
qui amende ADR-1cf0) : rattachement post → clip après relevé, versement des
statistiques dans le journal des résultats, métrique normalisée à maturité,
recalibrage automatique, coach sur déclencheur validé dans l'interface, bilan
des VOD de veille. Tout vit dans la bibliothèque `clipper/learning.py`
(ADR-b16b : rien sous `workspace/<video_id>/`, jamais d'import de
`clipper.web` ni d'une étape), état en JSON atomiques sous `state/learning/`
(ADR-35b7 §3, `channel_mod.atomic_write_json`), exécutée par le worker seul
(ADR-ca9a §5 par analogie), aucune valeur de secours silencieuse (ADR-ad2e),
tout LLM par `clipper.llm` (ADR-b1c1). Dates et âges en UTC, affichage en
heure de Paris (SPEC-5e50 R8). Aucun nom réel de compte ou de chaîne dans le
code, les tests et les entités.

## R0. Réglages `[learning]` (CONFIG_DEFAULTS de `clipper/learning.py`)
| clé | défaut | rôle |
|---|---|---|
| `enabled` | `true` | faux : la boucle ne fait rien (le worker n'appelle rien, l'écran le dit) |
| `state_dir` | `"state/learning"` | `links.json`, `sync.json`, `coach.json` |
| `link_window_h` | `12` | tolérance entre `tiktok_post.publish_at` et `posted_at` du post relevé |
| `maturity_days` | `3` | âge minimum (jours depuis la publication) d'un relevé pour que ses vues comptent |
| `window_days` | `90` | fenêtre des posts du compte servant de référence au rang |
| `min_account_posts` | `10` | posts mûrs à vues > 0 pour qu'un compte entre dans l'apprentissage |
| `coach_min_new_cases` | `10` | cas mûrs reliés nouveaux depuis le dernier passage du coach |
| `coach_min_interval_days` | `7` | délai minimum entre deux passages du coach |
| `veille_report_days` | `30` | profondeur du bilan de veille |
| `veille_report_max` | `20` | lignes de bilan au plus dans le prompt |
Un réglage hors domaine (entier négatif, `maturity_days` < 1,
`min_account_posts` < 2) est une `LearningError` à la lecture, jamais corrigé
en silence. Les réglages de `[outcomes]`, `[jury_calibration]`,
`[jury_coach]` restent ceux de leurs modules.

## R1. Rattachement post → clip (après relevé)
`link_posts(account, config=)` : pour chaque sidecar `output/<video_id>/
<clip_id>.json` dont `tiktok_post.account == account` et `tiktok_post.id` est
null, candidats = posts relevés du compte (`tiktok.read_history` puis
`merged_posts`, posts supprimés exclus) dont la légende correspond à la règle
de `tiktok.find_post_link` (légende + hashtags du sidecar et légende relevée
passées par `tiktok._squash`, l'une commençant par l'autre) et dont
`posted_at` est à moins de `link_window_h` heures de `tiktok_post.publish_at`,
hors posts déjà portés par l'id d'un autre sidecar du compte. Exactement un
candidat : `tiktok_post.id`, `tiktok_post.url`, `tiktok_post.linked_by =
"stats"`, `tiktok_post.linked_at` écrits dans le sidecar (note conservée) et
dans l'entrée de publication par `publish.attach_post` (qui refuse d'écraser
un `post_id` différent). Zéro ou plusieurs : rien d'écrit, entrée dans
`links.json` : `{"video_id", "clip_id", "account", "reason": "none" |
"ambiguous", "matches": [post_id...], "checked_at"}`. Un sidecar déjà pourvu
d'un id n'est jamais modifié. `links.json` porte aussi `last_run[account]`
(horodatage du dernier fichier de relevé traité) et les compteurs reliés /
non reliés par raison. `link_if_due(now, config=)` ne traite que les comptes
TikTok dont un fichier de relevé est plus récent que `last_run[account]`.

## R2. Versement dans le journal des résultats
`sync(now, config=)` : pour chaque clip relié (sidecar avec `tiktok_post.id`
présent dans les relevés du compte) : (a) une entrée `kind: "result"` une seule
fois par clip (clé `video_id/clip_id` dans `sync.json.results`) avec `qa` du
sidecar (`status`, `issues`) et `human_decision` null, portant `video_id`,
`clip_id`, `moment_id` ; (b) dès que le clip est mûr (R3), une entrée `kind:
"stats"` une seule fois par clip (clé dans `sync.json.scored`) portant
`video_id`, `clip_id`, `moment_id`, `post_id`, `account`, `posted_at`,
`fetched_at` du relevé retenu, `age_days`, et `stats = {views,
views_at_maturity, views_percentile, likes, comments, shares, avg_watch_s,
watched_full, new_followers}` (valeurs null gardées telles quelles, jamais 0
inventé). `moment_id = int(clip_id[:2])` (règle `captions._clip_id`, `NN` ou
`NN-pK`) ; un `clip_id` qui ne suit pas cette forme est une `LearningError`.
Journal append-only (`outcomes`), `sync.json` = `{"last_sync", "last_error",
"results": [...], "scored": [...], "excluded": [{video_id, clip_id, account,
reason}], "accounts": {account: {mature_posts, viewed_posts, eligible}}}`.
Un relevé déjà versé n'est jamais reversé (relance idempotente).

## R3. Maturité et métrique normalisée
Relevé retenu pour un post = le premier relevé du compte dont `fetched_at -
posted_at >= maturity_days` et dont le post porte `views` non null ;
`views_at_maturity` = ces vues. Référence du compte = tous ses posts relevés
(Clipper ou non) mûrs, `posted_at` dans `window_days` avant `now`.
`views_percentile` = rang fractionnaire 0-1 de `views_at_maturity` parmi la
référence (ex aequo = rang moyen ; référence d'un seul post → 0,5). Compte
avec moins de `min_account_posts` posts mûrs à `views_at_maturity > 0` :
aucune entrée `stats` pour ses clips, chacun listé dans `sync.json.excluded`
avec `reason = "account_below_min"` ; un clip non mûr : `reason =
"immature"` ; `rétention 3 s` et `partages` ne sont utilisés par aucune règle
tant que le relevé les rend null.

## R4. Déclenchement par le worker
`Worker.tick` appelle `learning.run_if_due(now, config=)` à chaque tour, avant
la veille (`_veille_due`) et après `_stats_due` : rien si `enabled` est faux ;
sinon `link_if_due` puis, si un fichier de relevé est plus récent que
`sync.json.last_sync`, `sync` puis R5 puis R6 (coach). Une `LearningError`,
`CalibrationError`, `CoachError` ou `ConfigError` est écrite dans
`sync.json.last_error` (`{"at", "where", "message"}`) et journalisée une fois
(`log.error`), jamais propagée hors du tour, jamais masquée ; le succès
suivant efface `last_error`. `clipper/web` n'appelle jamais `learning` (il lit
`state/learning/`).

## R5. Recalibrage automatique
Après un `sync` qui a ajouté au moins une entrée, `learning` appelle
`jury_calibration.calibrate(traces, config=, now=)` avec, pour chaque clip
relié dont `workspace/<video_id>/moments.json` existe et dont le moment
`moment_id` porte `jury.trace`, `{"video_id", "moment_id", "candidate":
{"trace": <moments[].jury.trace>}}` ; un clip sans `moments.json` ou sans
trace est compté dans `sync.json.calibration.untraced` (jamais une trace
inventée). Règles d'ADR-1cf0 inchangées (bornes, `min_clips`, lissage,
conformité et juges à veto fixes). `jury_calibration` : une entrée `stats` ou
`result` qui porte `video_id` et `moment_id` non nuls est reliée directement
(la recherche par `clip_id` ne sert qu'aux entrées sans eux) ; `stats_metric`
vaut `"views_percentile"` par défaut ; une entrée `stats` sans la métrique est
écartée et comptée dans `ignored_stats` avec `reason = "no_metric"` (pas une
erreur). `sync.json.calibration = {"at", "clips", "untraced", "weights_path"}`.

## R6. Coach sur déclencheur, jamais appliqué seul
`coach_if_due(now, config=)` ne tourne que si le nombre de clips reliés mûrs
(entrées `scored`) nouveaux depuis `coach.json.last_run` est au moins
`coach_min_new_cases` ET si `now - coach.json.last_run >=
coach_min_interval_days` (jamais tourné : seul le premier critère compte).
Cas = clips `scored` avec trace : `{"video_id", "moment_id", "text":
transcript du sidecar, "context": source_title + screen_title, "trace"}` ;
`judges` = perspectives actives de `jury` (config) ; appel
`jury_coach.propose(cases, rubric, judges, config=)` (règles de TASK-6595
inchangées : `min_cases`, plafond de leçons, similarité, rejeu, conformité
exclue). Chaque entrée rendue est consignée dans `coach.json.runs[]` :
`{"at", "cases", "judges": [{"judge", "accepted", "reason", "version",
"path", "metric", "status": "proposed" | "adopted" | "refused" | "rejected",
"decided_at", "decided_by"}]}` (`rejected` = refusée par le coach lui-même,
`proposed` = adoptable, en attente d'un humain). `learning` n'écrit jamais
dans `config.toml`, ni dans `clipper/jury.py`, ni dans le fichier de poids.

## R7. Validation humaine dans l'interface
`GET /api/learning` rend `{"enabled", "links", "sync", "weights" (contenu de
`state/jury_weights.json` ou null), "coach": [propositions `proposed`,
`adopted`, `refused` avec la perspective proposée lue dans
`prompts/jury/<juge>/vN.md`, la perspective en place, la métrique avant/
après]}`. `POST /api/learning/coach/{judge}/{version}/adopt` écrit la
perspective dans `[jury.judges.<juge>].perspective` de `config.toml` par
`config.write_config` (même chemin que `PUT /api/settings`, commentaires du
fichier préservés ou refus explicite comme Réglages), puis marque la
proposition `adopted` (`decided_at`, `decided_by = "web"`) ;
`.../refuse` marque `refused`. Proposition inconnue : 404 ; déjà décidée :
409 ; juge `conformite` : 409. Écran : section « Apprentissage » de l'écran
Statistiques (SPEC-c100 T1-T8) : état de la boucle (dernier versement, dernière
erreur en rouge, clips reliés / non reliés par raison, comptes exclus et
pourquoi), poids par juge (poids, accord, cas, raison), propositions du coach
(juge, version, métrique avant → après, perspective en place / proposée,
boutons « Adopter » et « Refuser », toast avec la réponse). Rien dans
l'interface ne déclenche un calcul ni un LLM.

## R8. Bilan de veille dans le prompt
Après chaque `sync`, `learning` écrit `state/veille/bilan.json` : `{"computed_at",
"days": veille_report_days, "entries": [{"picked_on", "candidate_id",
"source", "game_name", "channel_name", "title", "video_id",
"clips_published", "clips_mature", "views_percentile_mean" | null,
"views_at_maturity_max" | null, "missing": null | "no_clips" |
"not_published" | "immature" | "account_below_min"}]}` à partir de
`state/veille/seen.json` (`queued`, `veille_report_days` derniers jours, au
plus `veille_report_max` entrées, les plus récentes d'abord) et des sidecars
de `output/<video_id>/`. `veille._prompt` ajoute, avant « Candidats (VOD) »,
le bloc « Bilan des VOD choisies récemment (vues à maturité, rang 0-1 dans le
compte) » avec une ligne par entrée (`missing` écrit en clair), ou la ligne
« Bilan des VOD choisies récemment : aucun (pas encore de résultats) » si le
fichier est absent ; un fichier illisible est une `VeilleError`. Aucun chiffre
inventé.

## R9. Exclusions
Comptes `service = "youtube"` : hors boucle tant que `state/stats/youtube/`
n'existe pas (listés `excluded` avec `reason = "service_without_stats"`).
Juge `conformite` et juges à veto : jamais recalibrés ni coachés (ADR-1cf0).
Clips d'exploration (`exploration: true` dans `moments.json`) : versés et
calibrés comme les autres, marqués `exploration: true` dans l'entrée `stats`.

## R10. Tests, sans réseau ni navigateur ni vrai Claude
Fixtures sur `tmp_path` : sidecars, `state/stats/tiktok/<compte>/*.json`,
`state/publish/*.json`, `workspace/<video_id>/moments.json`,
`state/veille/seen.json` ; `clipper.llm.fake.FakeBackend` via
`llm.use_backend` pour le coach et le choix de veille ; `Worker` avec les
appels injectables. Chaque règle a au moins un test : rattachement unique,
ambigu, aucun, id déjà posé, post déjà pris ; versement idempotent (deux
`sync` = un seul jeu d'entrées), `moment_id` déduit, `clip_id` malformé ;
maturité (relevé trop jeune ignoré, premier relevé mûr retenu), rang 0-1 avec
ex aequo, compte sous le minimum exclu ; worker : appel à chaque tour, erreur
journalisée une fois, `enabled = false` ; calibration : traces lues de
`moments.json`, clip sans trace compté, liaison directe par `video_id`/
`moment_id`, `no_metric` ; coach : pas d'appel LLM sous le seuil ou avant
l'intervalle, proposition consignée `proposed`, `adopt` écrit la perspective
dans la config et marque `adopted`, `refuse`, 404/409 ; bilan : fichier écrit,
prompt de veille qui le contient, ligne « aucun » sans fichier, `VeilleError`
si illisible. `python -m pytest -q` vert.
