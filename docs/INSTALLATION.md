# Installation sans outils de développement

Ce guide s'adresse à qui veut faire un premier clip sans cloner le dépôt, sans
Python ni `uv` : juste un zip à dézipper et un double-clic. Pour l'installation
développeur (dépôt cloné, tests, `ank`), voir la section « Installation
développeur » du [`README.md`](../README.md).

## Prérequis

- Windows 10 ou 11, 64 bits.
- Un compte [Claude](https://claude.ai) (gratuit ou payant) : Clipper s'appuie
  sur Claude pour choisir les moments forts, écrire les titres et contrôler la
  qualité des clips (`clipper.llm`).
- Google Chrome, mais seulement si tu comptes publier sur TikTok ou YouTube
  depuis Clipper (vérifié à l'installation, jamais bloquant sinon).

Rien d'autre à installer à la main : Python, ffmpeg et le reste sont posés par
l'installeur, dans son propre dossier, sans toucher le reste de ta machine.

## Télécharger et dézipper

1. Va sur la page des [Releases](https://github.com/ZeDKouill3/TiktokClipper/releases)
   du dépôt et télécharge le fichier `Clipper-portable-<version>.zip` de la
   dernière version.
2. Dézippe-le où tu veux (Téléchargements, Bureau...) : le contenu du zip
   n'est qu'un point de départ, rien ne reste à cet endroit après
   l'installation.

## Installer.bat

Double-clique sur `Installer.bat`, dans le dossier dézippé.

> Windows peut afficher un avertissement SmartScreen (« Windows a protégé
> votre ordinateur ») parce que ce `.bat` vient d'être téléchargé et n'est pas
> signé : voir *Problèmes fréquents* plus bas pour l'ouvrir quand même.

Une fenêtre de console s'ouvre et affiche onze étapes numérotées
(`[n/11] ...`) : préparation des dossiers, installation de Python, de
l'environnement, de ffmpeg, de Claude, vérification de Chrome, préparation des
données, téléchargement des modèles, écriture du lanceur, puis un contrôle
final (`clipper doctor`). Chaque étape dit ce qu'elle fait ; un échec affiche
un message rouge en français avec le remède, jamais une étape sautée en
silence.

## Connexion à Claude

Si le CLI `claude` n'est pas déjà sur ta machine, l'installeur le pose tout
seul (étape 5) puis vérifie s'il est connecté à un compte. S'il ne l'est pas,
une fenêtre `claude auth login` s'ouvre : connecte-toi avec ton compte Claude,
ferme la fenêtre, puis reviens à la console Clipper et appuie sur une touche
pour continuer. Sans connexion à Claude, l'installation s'arrête : Clipper a
besoin de Claude dès la préparation d'une vidéo, il n'y a pas de mode dégradé
sans LLM.

## Où sont le programme et les données

L'installeur sépare toujours deux dossiers, qu'une mise à jour ou une
désinstallation ne traitent pas de la même façon :

- **Le programme**, par défaut `%LOCALAPPDATA%\Clipper\app` : Python,
  l'environnement, ffmpeg, le lanceur `Clipper.bat`. Jetable : une mise à jour
  le reconstruit, une désinstallation le supprime toujours.
- **Les données**, par défaut `Documents\Clipper` (ton dossier Documents) :
  `config.toml`, la grille de notation, et au fil de l'usage tes vidéos en
  cours, tes clips et ton journal. Jamais touché par une mise à jour, et
  conservé par défaut à la désinstallation.

Deux options d'`Installer.bat` changent ces emplacements : `--app <dossier>`
et `--data <dossier>`.

## Premier clip

À la fin de l'installation, la console Clipper s'ouvre toute seule dans ton
navigateur, à l'adresse `http://127.0.0.1:8000`. Le dossier de données
contient aussi un fichier `PREMIER-CLIP.txt` qui explique, pas à pas :
ouvrir la console depuis le raccourci `Clipper` du Bureau, coller l'URL d'une
vidéo YouTube ou d'une VOD Twitch dans l'écran « Vidéos », suivre la
progression, valider les moments retenus (mode « revue », par défaut) puis
récupérer le clip fini dans l'écran « Clips ».

## GPU

L'installeur détecte automatiquement une carte NVIDIA (`nvidia-smi`) : si elle
répond, l'extra `clipper[cuda]` est installé pour accélérer la transcription
et le rendu ; sinon, tout tourne sur CPU, seulement plus lentement. Rien à
régler à la main ; les options `--cpu` ou `--cuda` forcent un choix si la
détection automatique ne convient pas.

## Mise à jour

Télécharge le nouveau `Clipper-portable-<version>.zip` depuis les Releases,
dézippe-le et relance `Installer.bat` sans option : il retrouve seul les
dossiers choisis à la première installation (`--app`/`--data`, si tu les
avais changés). Le dossier programme est
recréé avec la nouvelle version ; ffmpeg et les modèles déjà téléchargés sont
conservés. Le dossier de données n'est ni lu ni modifié. Un zip plus ancien
que la version installée est refusé explicitement : pas de retour en arrière
silencieux.

## Désinstallation

Double-clique sur `Desinstaller.bat`, dans le dossier programme
(`%LOCALAPPDATA%\Clipper\app` par défaut) ou dans le zip dézippé. Il supprime
le dossier programme et le raccourci du Bureau. **Les données sont
conservées par défaut** : une question (`o/N`) te laisse choisir de les
supprimer aussi ; l'option `--donnees` les supprime sans demander. La
désinstallation est refusée tant que la console Clipper tourne (ferme-la
d'abord) ; elle ne touche jamais `claude`, Chrome ni les modèles déjà en
cache.

## Diagnostic : clipper doctor

En cas de doute, relance `Installer.bat` (double-clic, sans rien perdre) :
sa dernière étape lance `clipper doctor` et affiche le rapport à l'écran, une
ligne par point vérifié (Python, ffmpeg, Claude et sa connexion, Chrome,
GPU, modèles, configuration, dossiers de données) et dit précisément ce qui
manque.

## Problèmes fréquents

- **« claude n'est pas connecté »** — relance `claude auth login` dans un
  terminal, connecte-toi, puis relance `Installer.bat` (ou `clipper doctor`
  pour vérifier après coup). Sans Claude connecté, aucune vidéo ne peut être
  traitée (ADR-b1c1).
- **Chrome introuvable** — ce n'est qu'un avertissement : Clipper fonctionne
  sans Chrome pour créer des clips, Chrome n'étant nécessaire que pour
  publier sur TikTok ou YouTube depuis la console. Installe
  [Google Chrome](https://www.google.com/chrome) puis relance `clipper
  doctor` si tu veux publier plus tard.
- **Le port 8000 est déjà utilisé** — un autre programme (ou une console
  Clipper déjà ouverte) occupe ce port. Ferme l'autre programme, ou vérifie
  qu'une fenêtre Clipper n'est pas déjà ouverte en arrière-plan, puis relance
  `Clipper.bat`.
- **Windows bloque `Installer.bat` ou `Desinstaller.bat` (antivirus ou
  SmartScreen)** — ces fichiers viennent d'être téléchargés et ne sont pas
  signés numériquement, ce que Windows signale par prudence. Dans la fenêtre
  SmartScreen, clique sur « Informations complémentaires » puis « Exécuter
  quand même » ; si ton antivirus le met en quarantaine, restaure-le depuis
  son interface (le script ne fait rien d'autre que les étapes décrites dans
  ce document).
