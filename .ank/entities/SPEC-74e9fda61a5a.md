---
id: SPEC-74e9fda61a5a
type: spec
slug: mod-le-de-donn-es-de-la-console-v2-cha-nes-chann
title: "Modèle de données de la console v2 : chaînes ([channel]), file de traitement, publication, surveillance, état vidéo étendu (scope complet)"
created: 2026-10-01T10:55:26Z
author: nicoc@zedk_ordi
status: superseded
scope:
  - clipper/config.py
  - clipper/pipeline.py
  - clipper/web/app.py
  - clipper/channel.py
  - clipper/worker.py
  - clipper/publish.py
  - clipper/watch.py
references: [ADR-35b778a98d22, ADR-ad2e562b1810, ADR-b16b71007578, SPEC-6a476ca57f39]
supersedes: SPEC-fc0c156a8684
ratified: 203caefdbec5
verified:
  - by: nicoc@zedk_ordi
    at: 2026-10-01T10:56:28Z
schema: 4
version: 3
---

Successeur de SPEC-fc0c156a8684 : règles identiques, scope étendu aux modules qui portent ce modèle de données (clipper/channel.py, clipper/worker.py, clipper/publish.py, clipper/watch.py), oubliés à la ratification.

## Objet
Règles du modèle de données que l'interface v2 (ADR-4f6e) manipule : preset
de chaîne, file de traitement, état par vidéo, file de publication,
surveillance des sources. Tout est fichier (TOML sous `presets/`, JSON sous
`state/` et `workspace/<video_id>/`), écrit atomiquement (tmp + replace),
sans valeur de secours silencieuse (ADR-ad2e) : une entrée invalide est une
erreur nommée, jamais ignorée. Aucun nom réel : `ma_chaine`.

## 1. Chaîne et preset (`presets/<chaine>.toml`, clipper/channel.py)
1.1 Le nom de chaîne est le nom de fichier sans extension : `[a-z0-9_-]{1,40}`,
    unique, jamais renommé par l'interface (supprimer + recréer).
1.2 Le fichier est une surcouche de `config.toml` : `load_config(path,
    base=config.toml)` fusionne les clés plates puis chaque section clé par
    clé (le preset gagne, les sous-tables comme `[llm.usages.x]` sont
    remplacées entières), et valide le résultat comme un config complet
    (section inconnue, clé inconnue, mode invalide : ConfigError).
1.3 `[channel]` est validée par `CONFIG_DEFAULTS` de `clipper/channel.py` :
    - `display_name` (str, défaut = nom de fichier), `source_url` (str, page
      chaîne YouTube ou Twitch, vide autorisé = pas de source),
    - `watch` (bool, défaut false), `watch_interval_s` (int, défaut 1800),
      `watch_min_duration_s` (int, défaut 600 : VOD plus courtes ignorées),
    - `mode` (`review` | `auto`, défaut = mode global) : c'est lui, et non le
      mode global, qui s'applique aux vidéos de la chaîne,
    - `slots` (liste de `{day = "mon".."sun", time = "HH:MM"}` , défaut `[]`),
      `timezone` (str IANA, défaut `"Europe/Paris"`),
    - `tiktok_account` (str, défaut `""`, réservé à l'autopost, jamais lu
      avant),
    - `logo` (chemin PNG relatif au dépôt, défaut `""`).
1.4 Un preset sans table `[channel]` reste un preset valide pour la CLI ; pour
    l'interface, une chaîne est un preset qui a `[channel]`.
1.5 Écriture (interface ou `channel.save`) : le dict complet est sérialisé
    (`tomli_w`), relu par `load_config` avec la même base, et seulement alors
    remplace le fichier ; un ConfigError laisse le fichier intact et remonte.

## 2. File de traitement (`state/queue.json`, clipper/worker.py)
2.1 Liste ordonnée d'entrées `{id, video_id, url, channel | null, action
    ("run" | "render"), force_steps: [étapes], enqueued_at, status ("waiting"
    | "running"), pid | null}`. Une seule entrée `running` à la fois.
2.2 Ordre = `enqueued_at`, sauf « passer en tête » qui réordonne la liste
    (jamais l'entrée `running`). Doublon (même `video_id` et action déjà
    `waiting`) refusé avec erreur.
2.3 Le worker lance l'entrée de tête : `python -m clipper <action> <url|id>
    [--config presets/<channel>.toml] [--force-step x ...]`, enregistre
    `pid`, attend la fin, retire l'entrée. Annulation : signal de fin au
    processus enfant, entrée retirée, `pipeline.json` reçoit `status =
    "failed", reason = "annulée par l'utilisateur"`.
2.4 Au démarrage du worker, toute entrée `running` orpheline (pid mort)
    repasse `waiting` en tête. Une vidéo `queued` du pipeline (ADR-ad2e,
    `retry_at`) est reprise par le worker à l'heure dite, sans passer par
    `state/queue.json` (`pipeline.process_queue`).

## 3. État par vidéo (`workspace/<video_id>/pipeline.json`, clipper/pipeline.py)
3.1 Champs ajoutés au contrat existant : `channel` (str | null, chaîne dont le
    preset a servi), `enqueued_at`, `steps.<x>.progress` (null | `{fraction:
    0..1, eta_s: float | null, message: str}`), réécrit au plus toutes les
    2 s par le pipeline, remis à null quand l'étape finit.
3.2 Journal par vidéo : `workspace/<video_id>/events.jsonl`, une ligne
    `{at, level, step | null, message}` par transition d'état et par ligne
    de log INFO+ du pipeline pendant la vidéo ; jamais tronqué par le
    pipeline.
3.3 Relancer une étape : `force_steps` remet cette étape et toutes celles qui
    la suivent à `pending` (cache aval invalidé), les précédentes restent.

## 4. Publication (`state/publish/<chaine>.json`, clipper/publish.py)
4.1 Une entrée par clip : `{video_id, clip_id, series_id | null, part | null,
    status, slot_at | null (ISO, fuseau de la chaîne), decided_at,
    published_at | null, error | null}` ; `status` ∈ `approved`,
    `scheduled`, `published`, `failed`, `rejected`.
4.2 Approuver un clip : entrée `approved` puis, si la chaîne a des `slots`,
    attribution immédiate du prochain créneau libre après maintenant →
    `scheduled`. Les parties d'une série prennent des créneaux consécutifs
    dans l'ordre des parties ; refuser une partie passe toute la série en
    `rejected` (règle QA existante). Sans `slots`, le clip reste `approved`
    et le calendrier le montre « sans créneau ».
4.3 Déplacer = changer `slot_at` vers un créneau libre (un clip par créneau
    et par chaîne ; conflit = erreur). Marquer publié à la main = `published`
    + `published_at`. Tant qu'il n'y a pas d'API, aucune entrée ne passe
    `published` sans action humaine.
4.4 Un clip absent de `state/publish/` est « à valider ». Un clip dont le
    sidecar dit `ready = false` ne peut pas être approuvé (erreur nommée).
4.5 Éditions : `description` et `hashtags` sont modifiés en place dans le
    sidecar (SPEC-6a47) avec `edited_at` ; modifier `screen_title` relance
    render puis qa pour ce seul clip (file de traitement, action `render`,
    `force_steps = [render, qa]`, option clip ciblé), le sidecar est réécrit
    par le rendu. Une entrée `scheduled`/`published` n'est pas éditable sans
    repasser `approved`.

## 5. Surveillance (`state/watch/<chaine>.json`, clipper/watch.py)
5.1 Pour chaque chaîne `watch = true`, toutes les `watch_interval_s`, le
    worker liste les VOD de `source_url` par yt-dlp (extraction plate, sans
    téléchargement) ; les VOD de durée < `watch_min_duration_s` et celles déjà
    dans `seen` sont ignorées.
5.2 Fichier : `{checked_at, seen: [video_id...], pending: [{video_id, url,
    title, duration_s, published_at, found_at}], last_error | null}`.
5.3 Une VOD nouvelle : chaîne en mode `auto` → mise en file (`state/queue.json`,
    action run) et ajoutée à `seen` ; mode `review` → ajoutée à `pending` (« à
    confirmer » sur le tableau de bord) ; confirmer = mise en file, ignorer =
    `seen` seulement.
5.4 Une erreur de listage (réseau, yt-dlp) est écrite dans `last_error`, la
    chaîne reste surveillée, rien n'est inventé. Aucun test n'appelle
    yt-dlp : le listeur est injecté.
