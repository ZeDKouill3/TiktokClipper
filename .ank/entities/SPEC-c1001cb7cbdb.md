---
id: SPEC-c1001cb7cbdb
type: spec
slug: r-gles-de-l-interface-de-gestion-v2-crans-action
title: "Règles de l'interface de gestion v2 : écrans, actions, erreurs visibles, temps réel, raccourcis, accès"
created: 2026-09-30T20:40:18Z
author: w-plan-web
status: superseded
scope:
  - clipper/web/**
references: [ADR-09ad233678f2, ADR-4f6ed60e24e8, ADR-ad2e562b1810, SPEC-fc0c156a8684]
ratified: e2cb1dbcc5ee
verified:
  - by: nicoc@zedk_ordi
    at: 2026-09-30T21:07:00Z
schema: 4
version: 3
---

## Objet
Ce que l'interface (clipper/web/, ADR-09ad + ADR-4f6e) montre et permet, écran
par écran, et les règles transverses. Le style visuel (couleurs, typographie,
animations) n'est pas normé ici : il vient de la maquette locale
`research/web-ui/maquette/` (hors dépôt) ; en son absence, une page sobre qui
respecte ces règles suffit. Tout texte d'interface en français. Aucun nom
réel dans le code, les fixtures ni les captures du dépôt (`ma_chaine`).

## Règles transverses
T1 L'API ne contient aucune logique vidéo/audio/LLM : elle lit `workspace/`,
   `output/`, `state/`, `presets/`, appelle `clipper.pipeline` (lecture
   d'état, décisions), `clipper.channel`, `clipper.publish`, `clipper.watch`
   et écrit dans `state/queue.json` via `clipper.worker`. Chaque écran a ses
   routes `/api/...` en JSON ; la page est statique, sans étape de build.
T2 Aucune valeur de secours silencieuse (ADR-ad2e) : toute erreur d'API
   (4xx/5xx) porte `{"detail": "..."}` en français et s'affiche (toast +
   place de l'objet) ; jamais de « — » à la place d'une donnée manquante sans
   en dire la cause.
T3 Temps réel : la page ouvre `/api/events` (SSE) ; chaque événement
   `{kind, id, at}` (`video`, `queue`, `publish`, `watch`, `worker`) fait
   recharger l'objet concerné, pas la page. Si le flux tombe, polling
   toutes les 5 s et bandeau « connexion perdue ». Une vidéo en cours montre
   l'étape courante, `progress.fraction` et l'ETA si le pipeline les donne.
T4 Toute action destructive ou coûteuse (annuler, relancer une étape,
   refuser un clip/une série, supprimer une chaîne) demande une confirmation
   ou offre « Annuler » dans le toast pendant 5 s quand l'inverse existe
   (décision de revue, approbation, refus).
T5 Notifications : toast pour chaque événement `video` qui passe `done`,
   `failed`, `awaiting_review`, `queued` ; notification navigateur en plus si
   l'utilisateur l'a autorisée (réglage local au navigateur).
T6 Écrans utilisables sur téléphone (≥ 360 px de large) : navigation par
   onglets bas, revue et clips lisibles, zones de toucher ≥ 44 px.
T7 Accès (ADR-4f6e §5) : sur une adresse autre que le bouclage, la page
   demande le jeton une fois, le garde en cookie ; toute route `/api` et
   `/media` refuse 401 sans jeton valide.
T8 États vides et chargements : chaque liste vide dit quoi faire (« Ajoute
   une vidéo », « Crée une chaîne ») ; un chargement montre un squelette,
   jamais une page blanche.

## Écrans
E1 Tableau de bord : vidéos `running` (étape, progression, ETA), file
   d'attente ordonnée (passer en tête, retirer), vidéos `failed` et `queued`
   avec `reason` et `retry_at`, VOD « à confirmer » (SPEC-fc0c §5.3), clips
   à valider (compte + accès), prochaines publications, coût LLM du jour et
   de la semaine (somme des `llm_usage.jsonl`, par usage), état matériel
   (device de `clipper.gpu`, VRAM utilisée si disponible, sinon « CPU »).
E2 Vidéos : liste filtrable (chaîne, statut, texte) ; ajout par URL avec
   choix de la chaîne (ou « sans chaîne » = config.toml) et action ; fiche :
   frise des 12 étapes avec statut, durée, raison d'échec, `progress` ;
   journal (`events.jsonl`, suivi en direct) ; « relancer depuis cette
   étape » (`force_steps`), « annuler », lien vers la revue ou les clips.
E3 Revue des moments (mode review) : lecteur de la source calé sur le
   moment, liste des moments avec score, `hook_text`, justification du jury,
   bornes début/fin ajustables sur une timeline (glisser + champs), accepter
   / refuser / ajuster, raccourcis A, R, J/K (suivant/précédent), espace
   (lecture), « lancer le rendu » actif seulement quand chaque moment a une
   décision (sinon la raison s'affiche).
E4 Clips : galerie 9:16 par vidéo et par chaîne, lecteur, sidecar
   (titre d'écran, description, hashtags, partie N/M, `qa_status`,
   `issues`), édition de description/hashtags (en place) et du titre
   d'écran (re-rendu, confirmé), approuver / refuser (série entière, T4),
   re-rendre, télécharger le mp4, copier la description + hashtags.
E5 Chaînes : liste (nom, source, surveillance, mode, prochains créneaux),
   création/édition d'un preset sous forme de formulaire par section
   (`[channel]`, agencement, titre/CTA/badge, sous-titres, grille), chaque
   champ affichant sa valeur héritée de `config.toml` quand le preset ne la
   redéfinit pas ; erreur de validation (SPEC-fc0c §1.5) affichée au champ ;
   éditeur d'agencement visuel (canevas 1080x1920, zones webcam/jeu/badge/
   sous-titres à glisser et redimensionner sur une image clé d'une vidéo de
   la chaîne, mêmes clés que `[reframe]`) ; aperçu du style des sous-titres
   sur une phrase d'exemple, rendu par le pipeline (jamais par l'API).
E6 Publication : par chaîne, file `approved`/`scheduled`, calendrier
   hebdomadaire des créneaux avec glisser-déposer entre créneaux libres,
   statut `published`/`failed`, boutons télécharger + copier la description,
   « marquer publié », « repasser en attente ».
E7 Statistiques : par clip (résultats importés par `clipper.outcomes`,
   décisions humaines, QA), coûts LLM par vidéo/usage/période, durée par
   étape (moyenne, dernière), import CSV des stats de plateforme.
E8 Réglages : `config.toml` (mode global, dossiers, backend LLM et modèle
   par usage, surveillance) en formulaire, écriture validée (ADR-4f6e §2) ;
   section « Accès » (hôte, jeton) en lecture seule avec la commande à
   lancer.
