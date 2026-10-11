---
id: TASK-faf1e4dffe2f
type: task
slug: audit-lot-r-d-p-t-public-sans-donn-es-personnell
title: "Audit lot R : dépôt public sans données personnelles (captures, listes de garde) et petits restes docs/outils"
created: 2026-10-10T18:30:47Z
author: nicoc@zedk_ordi
status: done
scope:
  - docs/assets/readme/*.webp
  - tests/test_readme_assets.py
  - tests/test_docs_installation.py
  - tests/test_release_docs.py
  - .gitignore
  - tools/readme_shots/capture.py
  - tools/setup.ps1
  - docs/INSTALLATION.md
  - installer/PREMIER-CLIP.txt
blocked_by: []
done_criteria: |
  Lot R de l'audit complet du 10/10 (contre-vérifié). Défauts : installeur-M10 (docs/assets/readme/stats-video-light.webp et clips-light.webp affichent un nom de compte TikTok réel et son identifiant, non floutés ; vérifier TOUTES les .webp du dossier en les regardant), tests-nom-réel (tests/test_readme_assets.py ~59 et ~219, tests/test_docs_installation.py ~85, tests/test_release_docs.py ~24 écrivent en clair, dans des listes de mots interdits, le pseudo d'une chaîne amie, le nom d'utilisateur Windows et un dossier personnel : la garde publie ce qu'elle interdit), installeur-M13 (.gitignore sans build/ ni dist/), installeur-M11 (tools/readme_shots/capture.py périmé), installeur-M12 (tools/setup.ps1 exige ank avant de créer .venv), installeur-M7 (docs/INSTALLATION.md ~113 et installer/PREMIER-CLIP.txt ~22 : « clipper doctor dans un terminal » inadapté au public visé). Détails : E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\installeur.md et contre-verif.md (lecture seule). Correctif : captures concernées floutées sur les zones d'identifiants (Pillow) ou régénérées sur les données de démo neutres ; listes de garde remplacées par des empreintes sha256 (les mots ne figurent plus en clair nulle part dans les fichiers suivis) ; .gitignore complété ; docs/outils corrigés. NE PAS réécrire l'historique git (décision : pas de purge). Critère : grep -rniI des mots sensibles (les tirer des listes actuelles AVANT de les remplacer, ne jamais les écrire dans un fichier suivi ni dans ank log : les citer comme « mot 1/2/3 ») sur git ls-files -> 0 ; les tests de garde passent avec la liste en empreintes et échouent si on réintroduit un des mots (test rouge démontré) ; les images modifiées gardent leurs dimensions.
criteria_by: creator
verify: [tests]
method: diagnose
proof:
  - type: test
    ref: local/db0d77de6bd4@669cdbb
    tree: scope/65e0baafd9a8
    criteria: e673c7c42cd1
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---
