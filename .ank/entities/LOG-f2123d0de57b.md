---
id: LOG-f2123d0de57b
type: log
title: "Reproduction mesuree (pytest -k a1010, -n 0) : 20 rouges / 1 temoin vert. I1 : cmd /c"
created: 2026-10-10T22:02:05Z
author: w-b2235d04aafd
scope:
  - installer/Desinstaller.bat
  - installer/desinstaller.ps1
  - installer/install.ps1
  - tests/test_installer.py
  - tests/test_installer_real.py
  - installer/Clipper.bat.template
about: TASK-b2235d04aafd
seq: 5
schema: 4
version: 1
---

 app\Desinstaller.bat cwd=app -> 'car il est en cours d utilisation', exit 1, app/raccourci/pointeur intacts. I5 : nvidia-smi.bat exit 9 + message -> '[cuda] detecte'. M1 : pointeur '{not json' -> stdout vide, ConvertFromJsonCommand brut. M2 : --data 'X\"' -> 'data=...Mes Docs"' puis Test-Path ArgumentException a l etape 7. Autres : messages bruts ou acceptation attendue par les rapports.
