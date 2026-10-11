---
id: TASK-5b54d0a590bb
type: task
slug: ui-v3-u1-vendoring-basecoat-idiomorph-pr-fixage
title: "UI v3 U1 : Vendoring Basecoat + Idiomorph, préfixage .v3, test de garde"
created: 2026-10-11T03:58:42Z
author: nicoc@zedk_ordi
status: open
scope:
  - tools/vendor_basecoat.py
  - clipper/web/static/vendor/**
  - tests/test_web_vendor.py
blocked_by: []
done_criteria: |
  python tools/vendor_basecoat.py --check vérifie les sha256 ; test : tout sélecteur du CSS vendored commence par .v3 sauf la liste blanche ; node --check passe ; test_no_external_resource_in_index_and_css passe ; VERSIONS cite 1.0.2 et 0.8.0 Contexte : ADR-a308c4cafb5b et SPEC-7715813cad25 (lire par ank show). Plan détaillé (ligne U1 du tableau, parties 3 et 5) : E:\ClaudeRandom\TiktokParseUpload\research\drafts\ui-v3\PLAN.md ; direction visuelle DIRECTION.md, maquettes cliquables maquettes/ (index.html, captures/) dans le même dossier : les lire, ne rien copier de research/ dans le dépôt (noms réels). Règles : une tâche ne supprime jamais un test sans le remplacer par un équivalent nommé ; aucune ressource externe ; heure de Paris ; aucun nom réel (fixtures ma_chaine, compte_alpha) ; le bloc CSS de l'écran est retiré de style.css entre ses délimiteurs /* >>> x */ … /* <<< x */.
criteria_by: creator
verify: [tests]
method: tdd
schema: 4
version: 1
---
