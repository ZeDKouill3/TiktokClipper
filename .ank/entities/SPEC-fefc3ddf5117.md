---
id: SPEC-fefc3ddf5117
type: spec
slug: veille-contr-le-webcam-sur-les-miniatures-des-vo
title: "Veille : contrôle webcam sur les miniatures des VOD avant le choix de Claude (complète R6 de SPEC-bdd9)"
created: 2026-10-12T09:27:13Z
author: w-618ea09c9ae3
status: accepted
scope:
  - clipper/veille.py
  - clipper/web/static/**
references: [SPEC-bdd9e0db8905]
ratified: d0213668915d
verified:
  - by: nicoc@zedk_ordi
    at: 2026-10-12T10:11:01Z
schema: 4
version: 3
---

## Objet
Complète R6 de SPEC-bdd9 (« un appel texte seul ») : avant le choix de Claude, un contrôle webcam sur les miniatures écarte les VOD sans webcam (une VOD à mini-carte prise pour la webcam a donné 26 clips faux). Dates Europe/Paris, aucun nom réel de chaîne dans le code, les tests et les entités.

## R1. Réglage
`webcam_check_max` dans `CONFIG_DEFAULTS` de `clipper/veille.py` : défaut `30`, entier >= 0, `0` = contrôle désactivé (aucun téléchargement, aucun appel, `llm_webcam.status = skipped`). Hors bornes : `VeilleError` nommant la clé.

## R2. Sélection et planche
Après la collecte et les écartements existants, avant le choix de Claude : les `webcam_check_max` premiers candidats, dans l'ordre déjà utilisé pour les présenter à Claude, sont contrôlés. Leur miniature est le `thumbnail_url` officiel déjà porté par le candidat (Helix / YouTube) : jamais de scraping. Téléchargement parallèle borné en nombre et en temps (délai `http_timeout_s`). Les images sont assemblées en UNE planche numérotée (numéro 1..N hors de l'image, lisible, en marge ou bandeau). Aucune image n'est stockée au-delà du relevé du jour (au plus un fichier de planche sous `state/veille/`, remplacé à chaque relevé, supprimé avec le fichier du jour).

## R3. Appel LLM
UN appel `llm.ask` usage `veille_webcam` (image fixe, ADR-b1c1). Schéma : `{items: [{n, webcam: oui | non | doute, reason}]}` ; `check` : numéros existants, tous présents, uniques. Consigne : webcam = la caméra filmant le streamer, incrustée sur le jeu ; une mini-carte, un widget, un personnage du jeu ne sont pas une webcam ; dans le doute répondre `doute`.

## R4. Effets
- `non` : candidat retiré du choix de Claude, ajouté à l'état du jour avec la raison « pas de webcam sur la miniature : <reason> » (`excluded.no_webcam` compte, liste `webcam_excluded: [{candidate_id, title, reason}]`), visible à l'écran Veille comme les autres exclusions.
- `doute`, ou miniature absente / illisible (dont le placeholder `404_processing` de Twitch) : candidat gardé, champ `webcam: doute` ou `inconnue`. `oui` : `webcam: oui`. Candidat non contrôlé (au-delà de `webcam_check_max`) : `webcam: inconnue`.

## R5. Échec (ADR-ad2e)
Échec de l'appel ou du téléchargement complet : aucun repli silencieux. `llm_webcam: {status: error, error, model}` écrit dans le fichier du jour et dans le journal ; tous les candidats restent dans le choix avec `webcam: inconnue`. Décision explicite : un échec du contrôle ne bloque jamais la veille, il se voit. Contrôle coupé par l'échéance globale du relevé (`veille_deadline_s`) : `status = deadline`, candidats gardés. États possibles : `ok | error | skipped | deadline`.

## R6. Écran Veille
Chaque proposition porte une pastille webcam (oui / doute / inconnue) ; les exclusions « pas de webcam » apparaissent avec les autres écartés.

## R7. Tests
`FakeBackend` + `llm.use_backend`, miniatures fausses injectées (aucun réseau) : planche construite et numérotée, exclusion `non`, `doute`, miniature absente, échec, réglage `0`, échéance.
