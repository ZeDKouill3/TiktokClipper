---
id: TASK-0f481d4a0d8f
type: task
slug: clipmaniaq3-publish-confirm-timeout-r-p-t-apr-s
title: "ClipManiaq3 : publish_confirm_timeout répété après « Programmer » (cause, puis correctif testé)"
created: 2026-10-30T07:18:39Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/tiktok.py
blocked_by: []
done_criteria: |
  La cause des arrêts publish_confirm_timeout de ClipManiaq3 (1ab74eb871e0) est établie avec des preuves tirées des captures et du journal, ou les hypothèses sont écartées une à une avec preuve ; si la cause est dans Clipper, un correctif avec test de régression unitaire (pytest passe en entier).
criteria_by: creator
verify: [tests]
method: diagnose
proof:
  - type: test
    ref: local/107e284f488d@b7bd9e6
    tree: scope/e1b1dfe0ee1a
    criteria: 85b07bfb95fa
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---

Faits (relevés MAIN 30/10) :
- ClipManiaq3 (compte TikTok EN, id 1ab74eb871e0) s'arrête sur « aucune preuve de publication après 60 s ([tiktok] publish_confirm_timeout_s) : ni navigation vers la page Publications ni message de confirmation ». 6 fois : 26/10 22:45 et 23:56, 27/10 11:54, 20:51 et 22:25, 28/10 08:16 (UTC, noms des captures).
- Captures : state/browser/1ab74eb871e0/captures/*-publish_unconfirmed.{png,html} (dépôt principal, pas le worktree). Sur la capture du 28/10 : formulaire rempli, « Programmer » sélectionné, 12:00, 2026-11-05, vérification de contenu « Aucun problème constaté », bouton « Programmer » visible, rien ne s'est passé après le clic.
- Le 28/10 l'échec arrive au PREMIER envoi après une nuit en pause : ce n'est pas une rafale.
- Fausse piste : le texte « Tu as atteint la limite de vérifications pour aujourd'hui » est dans 40 HTML de captures sur 50, tous comptes confondus : c'est un gabarit caché (data-show="false"), pas une preuve.
- Écart d'avance écarté : ClipManiaq3 a réussi 44 programmations à plus de 7 jours (jusqu'à 10 j).
- Les 3 comptes FR (même code) n'ont pas ce problème. Différences connues : ClipManiaq3 publie en anglais (légende, titres EN), suggestions de lieux « Great Britain », etc.
- Journaux : logs/journal-2026-10-2*.log du dépôt principal.

Pistes à vérifier (sans s'y limiter) : popup ou modale invisible sur la capture (comparer le HTML d'un succès et d'un échec du même compte, il y a des captures de succès ?), bouton désactivé ou clic intercepté, avertissement propre aux contenus EN / musique / droits, quota de programmations en attente par compte (TikTok limite le nombre de posts programmés ? compter ceux de ClipManiaq3 dans state/publish), date au-delà d'une limite.

Interdits : ne pas piloter le navigateur de ClipManiaq3 ni d'aucun compte (le serve tourne, verrou de pilotage) ; ne pas toucher state/ ni config.toml ; analyse sur fichiers + code + tests unitaires seulement. Si une vérification en réel est nécessaire, l'écrire dans le rapport : MAIN la fera.
