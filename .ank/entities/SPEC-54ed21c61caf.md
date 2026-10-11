---
id: SPEC-54ed21c61caf
type: spec
slug: installeur-portable-windows-contenu-du-zip-avec
title: "Installeur portable Windows : contenu du zip (avec `installer/overrides.txt`), étapes, dossiers app et données (pointeur d'installation, options relues séparément, données jamais sous app), GPU (nvidia-smi en succès et un nom de GPU), claude, Chrome, modèles, mise à jour (prévol avant toute suppression), désinstallation (depuis app), tests (succède à SPEC-38f7)"
created: 2026-10-11T00:20:54Z
author: nicoc@zedk_ordi
status: proposed
scope:
  - pyproject.toml
  - clipper/gpu.py
  - clipper/__main__.py
  - README.md
  - docs/versions.md
  - Clipper.bat
  - AGENTS.md
  - clipper/doctor.py
  - clipper/models.py
  - tests/test_doctor.py
  - tests/test_models.py
  - installer/**
  - tests/test_installer.py
  - tools/build_portable.py
  - tests/test_build_portable.py
  - docs/INSTALLATION.md
references: [ADR-e1dac9ba2284, ADR-ad2e562b1810, ADR-fb9bcb1e98f5]
supersedes: SPEC-38f7761891f6
schema: 4
version: 1
---

## Changements par rapport à SPEC-38f7
Mandat de la mission (installeur-M9) :
- R1 : le zip contient aussi `installer/overrides.txt` (une ligne :
  `opencv-python; sys_platform == 'never'`), passé à l'étape 3 en
  `uv pip install --override` ; la liste « exactement » le comptait pour
  absent. Introduit par le commit `2c5e71e` (TASK-4f1d7d1ee341, point I1 de la
  relecture d'alors), constaté par l'audit du 10/10 (installeur-M9) ;
  `tests/test_build_portable.py` entérine déjà la liste du code.
- R3.3 : la clause « passé explicitement en `--override` si uv ne le lit pas
  depuis une wheel » devient inconditionnelle : `uv pip install --python
  <app>\.venv\Scripts\python.exe --override <zip>\installer\overrides.txt
  <wheel>[cuda]` ; `uv pip install` ne lit `[tool.uv] override-dependencies`
  que dans un `pyproject.toml` trouvé en remontant depuis le dossier courant,
  jamais le cas chez l'utilisateur (même commit).

Hors mandat M9, **vérifiés dans le code de `main`** et introduits par le lot M
de l'audit du 10/10 (TASK-b2235d04aafd, commit `2dcd12f`, merge `f1298a3`),
ajoutés parce qu'un successeur remplace la spec entière et que ces phrases de
SPEC-38f7 ne décrivent plus le code ; l'orchestrateur peut les retirer :
- R2 : un **pointeur** `%LOCALAPPDATA%\Clipper\install.json` (même contenu
  que `app\install.json`) est écrit à l'étape 11, toujours à cet emplacement
  fixe, même avec `--app` personnalisé (antérieur au lot M : commit `2c5e71e`,
  point I5 d'alors ; constaté ici car R2 disait « rien n'est écrit
  ailleurs ») ; sans option, une relance relit le pointeur ; `--app` seul
  relit `data` dans `<app>\install.json` ; `--data` seul relit `app` dans le
  pointeur (installeur-I3) ; `data` égal à `app` ou sous `app` est refusé
  (installeur-I4) ; un chemin d'option terminé par `"` ou `\` est nettoyé, un
  caractère interdit est refusé par un message, un `install.json` ou pointeur
  illisible aussi (installeur-M1, M2).
- R3.1 / R6 : **prévol** avant toute suppression de `.venv` : `uv.exe`, la
  wheel et `installer\overrides.txt` du zip doivent exister et le port 8000
  doit être libre dès qu'un `.venv` existe (mise à jour ou première
  installation interrompue), sinon arrêt sans rien toucher ; `--dry-run`
  l'annonce (installeur-M5, M6).
- R4 : GPU détecté si et seulement si `nvidia-smi --query-gpu=name
  --format=csv,noheader` sort en **code 0 et renvoie au moins un nom** ;
  présent mais en échec ou muet → CPU, détail affiché à l'étape 3
  (installeur-I5). Remplace « répond (sortie non vide) ».
- R3.9 : `Clipper.bat.template` écrit `set "APP=..."` / `set "DATA=..."`
  (chemin contenant `&`, installeur-M4).
- R8 : `Desinstaller.bat` se copie dans `%TEMP%` et s'y exécute (le dossier
  `app` est son dossier courant en double-clic et il est supprimé par le
  script), `--app` sans valeur est une erreur nommée ; sans `--app`,
  l'installation désinstallée est d'abord celle **à côté du script**
  (`<app>\install.json`), puis celle du pointeur, puis le dossier par défaut ;
  un dossier sans `install.json` ni `version.txt` est refusé
  (installeur-I1, I2, M3).
- R9 : tests `test_a1010_*` de `tests/test_installer.py` (un par défaut
  ci-dessus) ; le test réel désinstalle par `app\Desinstaller.bat` depuis
  `app`, sans `--app`.

Tout le reste est repris à l'identique de SPEC-38f7.

---

Règles de l'installeur portable (ADR-e1dac9ba2284). Public : une personne sous Windows 10/11 64 bits sans aucun outil de développement, qui veut faire un premier clip. Tous les messages sont en français et disent quoi faire.

## R1 — Contenu du zip `Clipper-portable-<version>.zip`
Produit par `tools/build_portable.py` (Python, lanceur `tools/build-portable.ps1` facultatif). Racine du zip = dossier `Clipper-portable-<version>/` contenant exactement : `Installer.bat`, `Desinstaller.bat`, `installer/install.ps1`, `installer/desinstaller.ps1`, `installer/Clipper.bat.template`, `installer/PREMIER-CLIP.txt`, `installer/overrides.txt` (fichier d'overrides `uv`, une ligne `opencv-python; sys_platform == 'never'`, livré dans le zip et jamais généré à l'installation : voir R3.3), `installer/clipper.ico`, `uv.exe` (version épinglée dans le builder, téléchargée depuis la GitHub Release d'astral-sh/uv, sha256 vérifié), `clipper-<version>-py3-none-any.whl` (produite par `uv build --wheel`), `version.txt` (= `[project] version`). Jamais de Python, de site-packages, de ffmpeg ni de modèle dedans. Taille < 150 Mo. Le builder prend en paramètres injectables le téléchargeur et le chemin de la wheel, pour être testé sans réseau.

## R2 — Dossiers
- `app` : par défaut `%LOCALAPPDATA%\Clipper\app`, remplaçable par `Installer.bat --app <dossier>`. Contient : `uv.exe`, `python/` (UV_PYTHON_INSTALL_DIR), `.venv/`, `ffmpeg/bin/` (ffmpeg.exe, ffprobe.exe), `Clipper.bat` (lanceur généré), `Desinstaller.bat`, `version.txt`, `installer.log`.
- `data` : par défaut `%USERPROFILE%\Documents\Clipper`, remplaçable par `--data <dossier>`. Contient `config.toml`, `rubric.toml`, `PREMIER-CLIP.txt`, puis `workspace/`, `output/`, `state/`, `logs/` créés par clipper à l'usage.
- Les deux chemins sont enregistrés dans `app\install.json` (app, data, version, cuda: true|false, date) et dans un **pointeur** `%LOCALAPPDATA%\Clipper\install.json` de même contenu, toujours à cet emplacement fixe même avec `--app` personnalisé. Une relance les relit comme défauts, option par option : sans `--app` ni `--data`, le pointeur donne les deux ; `--app` seul reprend `data` dans `<app>\install.json` ; `--data` seul reprend `app` dans le pointeur. Un pointeur ou `install.json` illisible est une erreur nommée (fichier et remède), jamais une exception brute.
- `data` égal à `app` ou situé sous `app` est refusé avec un message (la désinstallation effacerait les données avec le programme) ; `C:\x` et `C:\xy` restent deux dossiers voisins acceptés. Un chemin d'option terminé par un guillemet ou un antislash (complétion PowerShell) est nettoyé ; un caractère interdit (`| < > "`…) est refusé par un message.
- Rien n'est écrit ailleurs sauf : le pointeur ci-dessus, le raccourci `Clipper.lnk` sur le Bureau, le cache des modèles (`~/.cache/clipper/`, cache Hugging Face) et ce que l'installeur officiel de `claude` écrit lui-même.

## R3 — Étapes de `Installer.bat` (install.ps1, Windows PowerShell 5.1, sans élévation, sans `&&`)
Dans l'ordre, chaque étape affichée `[n/N] …`, journalisée dans `app\installer.log` ; un échec = message rouge en français avec le remède et code de sortie non nul, jamais d'étape sautée en silence :
1. Préparation : si `app\version.txt` existe c'est une mise à jour (R6). **Prévol avant toute écriture ou suppression** : `uv.exe`, la wheel et `installer\overrides.txt` doivent être présents dans le zip, et, dès qu'un `app\.venv` existe (mise à jour, ou première installation interrompue), le port 8000 doit être libre ; sinon arrêt avec message, rien n'a été modifié (`--dry-run` affiche ce qui arrêterait une vraie installation). Puis crée `app` et `data`, supprime un `.venv` existant.
2. Python 3.11 via `uv python install 3.11` avec `UV_PYTHON_INSTALL_DIR=app\python`, `UV_CACHE_DIR=app\cache` (supprimé en fin d'installation).
3. Environnement : `uv venv app\.venv --python 3.11` puis `uv pip install --python app\.venv\Scripts\python.exe --override <zip>\installer\overrides.txt <wheel>` (ou `<wheel>[cuda]`, R4). L'override est toujours passé explicitement : `uv pip install` ne lit `[tool.uv] override-dependencies` que dans un `pyproject.toml` trouvé en remontant depuis le dossier courant, jamais le cas chez l'utilisateur ; sans lui scenedetect tire `opencv-python` et mediapipe `opencv-contrib-python` dans le même `cv2/`. Résultat vérifié : exactement un paquet OpenCV installé (un seul `opencv*.dist-info` dans `site-packages`) et `python -c "import cv2"` réussit ; sinon arrêt. Le détail de la décision GPU (R4) est affiché à cette étape.
4. ffmpeg : si `app\ffmpeg\bin\ffmpeg.exe` absent, téléchargement d'une archive ffmpeg Windows 64 bits (URL et sha256 épinglés dans install.ps1), extraction de `ffmpeg.exe` et `ffprobe.exe` seulement. ffmpeg du PATH système jamais requis.
5. claude : si `claude` absent du PATH, exécution de l'installeur officiel natif (`irm https://claude.ai/install.ps1 | iex`), puis `claude auth status` ; non connecté → ouverture de `claude auth login` dans une fenêtre, attente, nouveau `claude auth status` ; toujours non connecté → arrêt avec message (le reste de l'installation ne reprend pas sans LLM, ADR-b1c1, ADR-ad2e).
6. Chrome : vérifié aux emplacements usuels (mêmes candidats que `clipper.browser.find_chrome`) ; absent → avertissement jaune avec lien, non bloquant (requis seulement pour publier).
7. Données : `clipper init` dans `data` seulement si `config.toml` absent (jamais `--force`) ; copie de `PREMIER-CLIP.txt`.
8. Modèles : `clipper models prefetch` (R5) ; échec réseau → arrêt explicite (une relance reprend là).
9. Lanceur : `Clipper.bat` écrit depuis `Clipper.bat.template` avec les chemins absolus de app et data (`set "APP=…"`, `set "DATA=…"` : un chemin contenant `&` reste valide) ; il met `app\ffmpeg\bin` et `app\.venv\Scripts` en tête du PATH, se place dans `data`, démarre `clipper serve` s'il n'écoute pas déjà et ouvre `http://127.0.0.1:8000` (même logique que le `Clipper.bat` du dépôt). Raccourci `Clipper.lnk` sur le Bureau (icône `clipper.ico`, dossier de travail `data`), remplacé s'il existe.
10. Contrôle : `clipper doctor` (R7) dans `data` avec le PATH du lanceur ; code non nul → arrêt avec son rapport.
11. Fin : écriture de `app\install.json` et du pointeur `%LOCALAPPDATA%\Clipper\install.json`, puis de `app\version.txt`, suppression de `app\cache`, ouverture de la console via `Clipper.bat`. Message vert avec les deux chemins.

Options : `--app`, `--data`, `--cpu`, `--cuda`, `--sans-console` (ne pas ouvrir la console à la fin), `--dry-run` (affiche le plan complet des étapes avec chemins et décisions, n'écrit rien, ne télécharge rien). `--dry-run` est le mode des tests par défaut.

## R4 — GPU
pyproject déclare un extra `cuda` = `nvidia-cublas-cu12`, `nvidia-cudnn-cu12` (versions compatibles ctranslate2 >= 4.8 / cuDNN 9, épinglées en minimum). L'installeur installe `[cuda]` si et seulement si `--cuda` est passé, ou si `nvidia-smi` est dans le PATH **et** que `nvidia-smi --query-gpu=name --format=csv,noheader` sort en code 0 **et** renvoie au moins un nom de GPU ; `--cpu` l'interdit. Un `nvidia-smi` présent mais en échec (code non nul, injoignable) ou qui ne renvoie aucun nom vaut CPU, et la raison (« nvidia-smi present mais en echec (code N) : aucun GPU detecte », …) est affichée à l'étape 3. Sans GPU, une ligne dit « CPU : transcription plus lente ». `clipper.gpu` ajoute au PATH du processus (et `os.add_dll_directory`) les dossiers `nvidia\*\bin` présents dans le site-packages courant avant d'interroger ctranslate2 ; absents → rien à ajouter, CPU (ADR-fb9b). Jamais 2 Go téléchargés sans GPU détecté.

## R5 — Modèles
`clipper models prefetch` télécharge dans leurs caches habituels le modèle mediapipe (`clipper.reframe`, `~/.cache/clipper/blaze_face_short_range.tflite`) et le modèle faster-whisper de `[transcribe] model` (défaut `small`), via la même fabrique que l'étape transcribe ; affiche ce qui était déjà présent ; échec = erreur explicite, code non nul. Testé par défaut avec une fabrique simulée (aucun réseau).

## R6 — Mise à jour
Relancer `Installer.bat` d'un zip de version >= la version installée : `app\.venv` recréé (suppression puis R3.3), `python/`, `ffmpeg/` et les modèles conservés s'ils sont présents et valides, `Clipper.bat`, raccourci, `install.json` et pointeur réécrits, `data` jamais lu ni écrit sauf R3.7 (qui ne fait rien si `config.toml` existe). Version plus ancienne que l'installée → refus explicite (pas de rétrogradation silencieuse). La console en cours (`clipper serve`) doit être fermée : port 8000 occupé → arrêt avec message avant toute suppression ; de même un zip incomplet (R3.1, prévol) arrête avant toute suppression : une mise à jour ne détruit jamais une installation qui marche pour échouer ensuite.

## R7 — `clipper doctor`
Commande `clipper doctor [--json]` dans `clipper/doctor.py` (ni étape du pipeline ni web : un module utilitaire, qui n'importe aucune étape, ADR-b16b). Vérifie et affiche une ligne par point : Python et version de clipper, ffmpeg/ffprobe (trouvés où), `claude` (trouvé où, `claude auth status` connecté ou non), Chrome (chemin ou absent), GPU (device de `clipper.gpu`, paquets nvidia présents ou non), modèles (mediapipe, whisper configuré : présents ou non), `config.toml` lisible dans le dossier courant, dossiers de données écrivables. Code de sortie 0 si tout ce qui est requis pour un premier clip est là (ffmpeg, claude connecté, config, modèles), 1 sinon ; Chrome et GPU ne sont que des avertissements. Toutes les sondes sont injectables (fonctions `which`, lanceur de sous-processus, chemins) pour des tests sans réseau ni binaire.

## R8 — Désinstallation
`Desinstaller.bat` (copié dans `app` par R3.9, le chemin documenté) se copie d'abord dans `%TEMP%` et s'y exécute (en double-clic, `app` est son dossier courant et il est supprimé par le script ; la copie s'efface à la fin), puis appelle `desinstaller.ps1` avec ses options. `desinstaller.ps1` refuse si la console tourne (port 8000) ; détermine l'installation : `--app <dossier>` si donné (`--app` sans valeur = erreur nommée), sinon l'installation **à côté du script** (`<app>\install.json` existe), sinon celle du pointeur `%LOCALAPPDATA%\Clipper\install.json`, sinon `%LOCALAPPDATA%\Clipper\app` ; un dossier sans `install.json` ni `version.txt` est refusé (jamais un succès silencieux ni la suppression d'un dossier quelconque) ; un `install.json` ou pointeur illisible est une erreur nommée. Lit `app\install.json`, sort du dossier `app` puis supprime `app`, le pointeur et `Clipper.lnk` du Bureau ; demande explicitement (`o/N`, défaut non) avant de supprimer `data` ; option `--donnees` pour l'inclure sans question, `--dry-run` pour afficher sans agir. Ne touche jamais `claude`, Chrome ni les caches de modèles.

## R9 — Tests
Par défaut (sans réseau, sans téléchargement, sans vrai binaire) : builder testé avec téléchargeur simulé (contenu exact du zip, R1, `overrides.txt` compris) ; `install.ps1 --dry-run` et `desinstaller.ps1 --dry-run` exécutés par pytest avec `powershell -NoProfile -ExecutionPolicy Bypass` (skipif powershell absent) sur des dossiers temporaires, sorties comparées aux décisions attendues (CPU/CUDA selon un `nvidia-smi` simulé par PATH, dont un `nvidia-smi` en échec → CPU, première installation vs mise à jour, data existant jamais listé en écriture, refus de rétrogradation, options relues séparément, `data` sous `app` refusé, prévol et port avant toute suppression, chemins nettoyés ou refusés, pointeur et `install.json` corrompus, lanceur avec `&`, désinstallation trouvée à côté du script ou par le pointeur : tests `test_a1010_*`) ; `clipper doctor` et `clipper models prefetch` testés avec sondes et fabriques simulées. Un seul test réel optionnel, `CLIPPER_INSTALLER_REAL=1` : construit le zip, lance `Installer.bat --app <tmp> --data <tmp> --cpu --sans-console` sur un PC qui a déjà `claude` connecté, vérifie `clipper doctor` exit 0, une mise à jour, puis `app\Desinstaller.bat --donnees` lancé depuis `app` sans `--app`.

## R10 — Documentation
`docs/INSTALLATION.md` (parcours utilisateur : télécharger le zip de la Release, dézipper, double-clic `Installer.bat`, se connecter à Claude, premier clip, mise à jour, désinstallation, où sont mes clips) ; README : section « Installation sans outils de développement » en tête de l'installation actuelle (qui devient « Installation développeur ») ; `docs/versions.md` critère n°1 reformulé sur l'installeur ; CHANGELOG. Le zip est attaché à chaque GitHub Release par l'orchestrateur.
