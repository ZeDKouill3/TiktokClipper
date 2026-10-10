---
id: TASK-b2235d04aafd
type: task
slug: audit-lot-m-installeur
title: "Audit lot M : installeur"
created: 2026-10-10T18:25:25Z
author: nicoc@zedk_ordi
status: done
scope:
  - installer/Desinstaller.bat
  - installer/desinstaller.ps1
  - installer/install.ps1
  - tests/test_installer.py
  - tests/test_installer_real.py
  - installer/Clipper.bat.template
blocked_by: []
done_criteria: |
  Lot M de l'audit complet du 10/10 (contre-vérifié par Fable). Défauts couverts : installeur-I1, I2, I4, I3, M1, M3, M2, M5, M6, M4, I5. Détails, scénarios, preuves rejouables (scripts dans scratch-<domaine>/) : rapports E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\<domaine>.md (coeur, publication, web, jury, image, stats, media, veille, installeur ; id = <domaine>-I<n>/M<n>) et E:\ClaudeRandom\TiktokParseUpload\research\reviews\audit-1010\contre-verif.md (verdicts, lot M) ; LECTURE SEULE, ne rien écrire dans research/. Correctif attendu : `installer/desinstaller.ps1` (`Set-Location` hors `app`, `$App` depuis `$PSScriptRoot`/pointeur, `--app` sans valeur, `install.json` sous `try`), `installer/Desinstaller.bat` (`cd /d C:\Users\nicoc\AppData\Local\Temp`), `installer/install.ps1` (`--app` seul relit `<app>\install.json`, refus `data` ⊆ `app`, `nvidia-smi` code 0 + nom, pointeur sous `try`, `TrimEnd`, prévol avant suppression de `.venv`, port vérifié dès que `.venv` existe), `installer/Clipper.bat.template` (`set "APP=…"`), `tests/test_installer.py`, `tests/test_installer_real.py` (cwd = `app`). Critère CPU : Un faux `app` sous scratch : `Desinstaller.bat` lancé avec cwd = `app` et sans `LOCALAPPDATA` par défaut supprime `app`, le raccourci et le pointeur ; `install.ps1 --dry-run --app X --data X\data` échoue avec `Fail` nommant les deux chemins. Pour chaque défaut : test de régression ROUGE avant le correctif, vert après ; tests existants des modules touchés verts ; aucune valeur de secours silencieuse (ADR-ad2e) ; si un défaut s'avère faux ou exige une décision humaine (amendement de spec/ADR), le dire dans ank log et ne pas le corriger.
criteria_by: creator
verify: [tests]
method: diagnose
proof:
  - type: test
    ref: local/1b679d1e79bc@a459292
    tree: scope/ff7798aba7ba
    criteria: b21b0e12a65f
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 4
---
