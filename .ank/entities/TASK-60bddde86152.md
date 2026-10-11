---
id: TASK-60bddde86152
type: task
slug: ui-v3-u14-cl-ture-suppression-de-style-css-du-pr
title: "UI v3 U14 : Clôture : suppression de style.css, du préfixe .v3 (script --no-prefix), des attributs data-theme, de fonts.css inutilisés ; captures README en clair"
created: 2026-10-11T03:59:40Z
author: nicoc@zedk_ordi
status: open
scope:
  - tools/vendor_basecoat.py
  - clipper/web/static/**
  - README.md
  - docs/**
  - tests/test_web*.py
blocked_by: [TASK-d4123fd6829d, TASK-6047856a5558, TASK-1679e58d021b, TASK-e3dee736b676, TASK-f4b50350d8d1, TASK-3c87a337ffc2, TASK-fecba79ca3d4, TASK-cda961181d07, TASK-05d7b35de9ca, TASK-2f3bcef584ca, TASK-0c4a2f5767f4]
done_criteria: |
  style.css absent ; aucun class="screen" sans v3 puis retrait du mot v3 partout (grep) ; test_static_assets_are_served mis à jour ; captures README régénérées (hors CI) Contexte : ADR-a308c4cafb5b et SPEC-7715813cad25 (lire par ank show). Plan détaillé (ligne U14 du tableau, parties 3 et 5) : E:\ClaudeRandom\TiktokParseUpload\research\drafts\ui-v3\PLAN.md ; direction visuelle DIRECTION.md, maquettes cliquables maquettes/ (index.html, captures/) dans le même dossier : les lire, ne rien copier de research/ dans le dépôt (noms réels). Règles : une tâche ne supprime jamais un test sans le remplacer par un équivalent nommé ; aucune ressource externe ; heure de Paris ; aucun nom réel (fixtures ma_chaine, compte_alpha) ; le bloc CSS de l'écran est retiré de style.css entre ses délimiteurs /* >>> x */ … /* <<< x */.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---
