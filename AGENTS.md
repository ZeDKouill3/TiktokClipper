This repo uses Ank: tasks and decisions live in `.ank/`.

## Setup

```powershell
uv venv
uv pip install -e ".[test]"
```

`uv` est obligatoire (pas `pip` seul) : voir `[tool.uv] override-dependencies`
dans `pyproject.toml`, qui force un seul paquet OpenCV installé. `tools/setup.ps1`
fait cette installation et vérifie les prérequis (uv, Python 3.11, ffmpeg,
`claude`, `ank`, GPU optionnel).

## Tests

```powershell
pytest
```

Tout le pipeline doit tourner sur CPU pour les tests (ADR-fb9b) : aucun test
n'a besoin d'un GPU pour passer. Ce qui a réellement besoin du réseau, d'un
vrai modèle ou du vrai Claude est un test optionnel, sauté par défaut via
`skipif` (binaire absent du PATH, ou variable d'environnement à positionner
explicitement, ex. `CLIPPER_CLAUDE_INTEGRATION=1`, `CLIPPER_REAL_MODELS=1`) —
jamais lancé en CI ni par défaut en local.

Test réel de l'installeur portable (SPEC-54ed R9) : construit le vrai zip,
l'installe en CPU dans un dossier temporaire sous `research/installer-real/`
(jamais `%LOCALAPPDATA%\Clipper` ni le Bureau), vérifie `clipper doctor`, une
mise à jour, puis la désinstallation complète. Plusieurs minutes et ~700 Mo
téléchargés : jamais en parallèle d'un autre travail réseau/CPU lourd.

```powershell
$env:CLIPPER_INSTALLER_REAL = "1"
python -m pytest -q tests/test_installer_real.py
```

Test réel optionnel des candidats d'action (SPEC-b0f3 R17) : copie un extrait
de VOD (10 min au plus) dans un workspace temporaire et enchaîne transcribe,
audio, scenes, action puis moments en « transcript+action » avec la config
réelle (vrai whisper, vrai `claude`, quota consommé). Jamais par défaut ni en
CI : sauté sans les deux variables d'environnement.

```powershell
$env:CLIPPER_ACTION_REAL = "1"
$env:CLIPPER_ACTION_REAL_VIDEO = "C:\chemin\extrait.mp4"
python -m pytest -q tests/test_action_real.py
```

## Règles ank

- CLI ank seulement : jamais lire ou écrire `.ank/` à la main, cet état est
  opaque comme `.git/` (`ank show`/`ank find`/`ank context` savent lire).
- `ank accept` (ratifier un ADR) est réservé à un humain, sur la branche
  par défaut, signé — jamais un agent.
- Un worktree par agent, une branche par tâche coupée depuis la branche par
  défaut ; `ANK_AGENT` identifie la session (sinon `<user>@<hostname>`, un
  mode dégradé où deux sessions partagent une claim au lieu de se
  l'arbitrer).
- `ank done` doit trouver `ank` (et les outils de test) dans le PATH :
  ajoute `.venv\Scripts` en tête avant de le lancer, par exemple
  `$env:Path = "$PWD\.venv\Scripts;" + $env:Path; ank done`.

## Décisions ratifiées (ADR / SPEC)

Liste régénérée depuis `ank find --type adr --status accepted` et `ank find --type spec --status accepted` (statuts exacts). Les specs remplacées (superseded) ne sont pas listées : leur successeur l'est.

### ADR acceptés

- **ADR-b16b** — pipeline `clipper/` (Python >= 3.11) : une étape = un module
  qui lit ses entrées et écrit sous `workspace/<video_id>/` ; une étape déjà
  faite ne se relance pas sauf `--force` ; une étape n'importe jamais une
  autre étape ni `clipper.web` directement, seul `clipper.pipeline` enchaîne.
- **ADR-fb9b** — device résolu par `clipper.gpu` (jamais codé en dur) ; un
  seul modèle lourd en VRAM à la fois, libéré explicitement après usage.
- **ADR-b1c1** — tout appel LLM passe par `clipper.llm` (backends
  interchangeables, modèle par usage, réponse validée contre un schéma JSON).
  Texte et images fixes seulement, jamais vidéo/audio.
- **ADR-ad2e** — mode `review`/`auto` en config ; aucune valeur de secours
  silencieuse (légende générique, moments par défaut, backend dégradé) : un
  échec remonte, est journalisé, ou met la vidéo en attente.
- **ADR-49cd** — interface web (`clipper/web/`) : page statique servie par
  FastAPI, aucune logique de traitement vidéo/audio/LLM dedans ; seule
  exception, une liste fermée de fonctions pures de validation de config des
  étapes (`moments.resolve_rubric_path`, `reframe._settings`,
  `render.check_cta_handle_gap`...). Succède à ADR-09ad.
- **ADR-e1da** / **SPEC-54ed** (succède à SPEC-38f7) — installeur portable Windows : zip
  d'amorçage (`uv.exe` + wheel + `installer/`) construit par
  `tools/build_portable.py` ; programme sous `%LOCALAPPDATA%\Clipper\app`
  (jetable, refait à chaque mise à jour), données sous `Documents\Clipper`
  (jamais touchées par une mise à jour) ; CUDA seulement si un GPU NVIDIA
  est détecté ; `claude` installé par son installeur officiel ; `clipper
  doctor` vérifie l'installation ; aucun repli silencieux.
- **ADR-1cf0** — apprentissage du jury à partir des erreurs, sans
  uniformisation : vérité terrain = signaux réels, poids des juges bornés,
  prompts retouchés par lots et jamais au fil de l'eau, exploration, juge
  conformité hors apprentissage.
- **ADR-c260** — boucle d'apprentissage branchée sur les relevés réels
  (amende ADR-1cf0) : `clipper/learning.py`, rattachement post→clip, versement
  stats→outcomes, recalibrage, coach validé dans l'interface.
- **ADR-ff87** — jury de juges IA pour les décisions de jugement du mode
  auto : au moins 3 juges indépendants et anonymes, débat ciblé sur les
  divergences, veto motivé du juge conformité.
- **ADR-a308** — console v3 : composants Basecoat et Idiomorph copiés dans
  `clipper/web/static/vendor/` (versions et sha256 épinglés, sélecteurs
  préfixés `.v3` pendant la migration), aucune classe utilitaire ni build,
  icônes Lucide et polices locales, thème clair par défaut.
- **ADR-35b7** — console de gestion web v2 : worker séparé qui traite une
  vidéo à la fois, presets par chaîne en surcouche, SSE, jeton d'accès local.
- **ADR-4e57** — candidats d'action pour les VOD gaming : étape `action`
  (pics audio, densité de plans, images décrites par le LLM) entre `scenes`
  et `moments` ; moments en `transcript+action` selon le style.
- **ADR-ca9a** — veille des sujets chauds : `clipper/veille.py` (bibliothèque,
  pas une étape), sources officielles seulement (Twitch, YouTube, Steam),
  état sous `state/veille/`, exécution par le worker seul, meilleurs clips
  du jour archivés, jamais supprimés.
- **ADR-798c** — veille : IGDB (API officielle de Twitch) source des dates de
  sortie et de la hype, avec le même jeton d'app Twitch que Helix.
- **ADR-0944** — veille : IGDB fournit aussi les champs d'affichage d'un jeu ;
  jaquettes affichées par URL directe `images.igdb.com`, jamais stockées.
- **ADR-05a4** — veille : Steam officiel (joueurs simultanés, joueurs par
  appid, abonnés via la page XML publique), plafonné et espacé.
- **ADR-f29e** — veille : historique de tendance sur 30 jours pour tout jeu
  retenu (avis Steam, VOD Twitch sur un mois), relevés propres, rien d'estimé,
  relevé dans un fil du worker qui ne bloque jamais la boucle ; test d'accès
  Twitch en parallèle borné (`twitch_access_workers`). Succède à ADR-6e21.
- **ADR-1a58** — publication et statistiques TikTok par pilotage d'un vrai
  navigateur (Playwright, profils persistants), en attendant l'API officielle.
- **ADR-58c0** — publication et statistiques YouTube (Shorts) par pilotage
  d'un vrai navigateur sur YouTube Studio, comme TikTok.

### SPEC acceptées

- **SPEC-6a86** — contrat de sortie d'un clip : titre d'écran sobre, sans
  emoji ni superlatif par défaut. Succède à SPEC-6a47 (reprise à l'identique
  pour le reste : format letterbox par défaut, `.mp4` + `.json` sidecar,
  appel à l'abonnement désactivé par défaut).
- **SPEC-b19b** — webcam du stream trouvée par période (rectangles candidats
  numérotés choisis par Claude, garde-fous locaux journalisés, recalage),
  visage exigé par clip sans bords réels, `empty_webcam` bloquant en
  `stream_split` ; reprend l'agencement `split` (webcam en haut, jeu en bas,
  badge optionnel, sous-titres réglables) ; sur une période unique, jamais de
  garde-fou contre la réponse de Claude. Succède à SPEC-5b9a (lignée
  SPEC-4a9b, SPEC-76dc).
- **SPEC-4063** — grille de notation des moments v4 (plafond souple par heure
  avec plancher `min_moments_cap`). Succède à SPEC-53f3.
- **SPEC-9216** — grille gaming embarquée (`builtin:gaming`), choisie par
  chaîne.
- **SPEC-b0f3** — grille gaming-action embarquée (`builtin:gaming-action`,
  seuil éliminatoire) et candidats d'action par passages, choisis par style.
- **SPEC-73d0** — jury : chaque juge donne sa confiance par moment, prise en
  compte dans le débat et l'agrégation.
- **SPEC-040f** — boucle d'apprentissage : rattachement post→clip après
  relevé (couples symétriques, posts supprimés exclus), métrique à maturité,
  recalibrage automatique, coach validé dans l'interface. Succède à SPEC-00db.
- **SPEC-1ed3** — publication pilotée depuis l'écran Publication : choisir le
  clip, le compte, maintenant ou programmé, et tous les réglages.
- **SPEC-6076** — TikTok par navigateur v2 : aucun compte dans un preset, le
  compte est choisi à chaque publication.
- **SPEC-5e50** — YouTube Shorts par navigateur : comptes YouTube, publication
  immédiate ou programmée, statistiques à l'usage, heure de Paris partout.
- **SPEC-47e2** — statistiques TikTok : tableau de bord par compte alimenté
  uniquement par le relevé de TikTok Studio, relevé seulement quand Clipper
  est utilisé.
- **SPEC-6fa4** — comptes : carnet local (e-mails, mots de passe dans le
  coffre de l'OS via keyring), générateur de mot de passe, jamais de repli
  silencieux.
- **SPEC-f348** — comptes = comptes de publication : connexion vérifiée,
  « prêt à publier » automatique, pause manuelle d'un compte, compte choisi
  par publication.
- **SPEC-1548** — modèle de données de la console v2 : chaînes, file de
  traitement (un seul worker, reprises par la file), publication, surveillance,
  état vidéo étendu. Succède à SPEC-74e9.
- **SPEC-7715** — règles de l'interface de gestion v3 : décision d'abord,
  tiroirs et dialogues qui gardent le contexte, palette et raccourcis clavier,
  temps réel par rapprochement du DOM, erreurs en place, thème clair par
  défaut, mobile. Succède à SPEC-c100.
- **SPEC-bdd9** — veille : réglages `[veille]`, fichiers sous `state/veille/`,
  sources, candidats, choix de Claude, actions Clipper/Ignorer, meilleurs
  clips du jour archivés, écran Veille.
- **SPEC-8797** — veille : historique de tendance sur 30 jours, séries à
  trous jamais estimées, test d'accès Twitch par jeu (parallèle borné), relevé
  dans un fil du worker avec échéance globale. Succède à SPEC-85a0.
- **SPEC-8a45** — veille : un relevé rejoué le même jour remplace la liste des
  propositions du jour, décidées comprises.
- **SPEC-df51** — veille : calendrier des sorties de jeux (IGDB, J-15..J+14),
  Steam officiel à la place de SteamDB, filtre de communauté, au plus N VOD
  par jeu.
- **SPEC-6d1f** — répartition automatique du lendemain : plan par compte TikTok
  calculé chaque soir, modifiable, validé d'un clic (rien ne part sans) ; bonus
  sur posts d'au moins 24 h, exploration réservée, vivier par compte, refus
  explicites. Succède à SPEC-78dc.

### SPEC proposée (pas encore `ank accept`)

- **SPEC-2a1e** — doublon vide créé par erreur (corps absent), à ne pas
  ratifier : la vraie spec est SPEC-0eec, remplacée par SPEC-4063.

## Conventions

- Les réglages d'un module vivent dans son `CONFIG_DEFAULTS` (dict au niveau
  module) ; c'est ce dict qui rend une table `[nom]` de `config.toml`
  valide — voir `clipper/config.py`. Ne pas ajouter de réglage ailleurs
  (variable globale, argument caché...).
- Device via `clipper.gpu.get_device()`, jamais `"cuda"`/`"cpu"` en dur.
- Tout accès à un LLM via `clipper.llm.ask(...)` ; dans les tests, brancher
  `clipper.llm.fake.FakeBackend` avec `llm.use_backend(fake)` — jamais le
  vrai Claude dans un test qui tourne par défaut.
- Aucun test n'utilise le réseau. Un test qui a besoin d'un vrai modèle, du
  GPU ou du vrai Claude est marqué `skipif` et sauté par défaut.
- Une étape (module `clipper/x.py`) n'importe jamais une autre étape ni
  `clipper.web` : l'enchaînement passe uniquement par `clipper.pipeline`.

## Pièges

- **Installer avec `uv`**, pas `pip` seul : `pip` ignore
  `[tool.uv] override-dependencies` et peut installer deux paquets OpenCV
  incompatibles (mediapipe veut `opencv-contrib-python`, scenedetect veut
  `opencv-python`, même module `cv2`).
- **faster-whisper en CUDA** a besoin des paquets `nvidia-cublas-cu12` et
  `nvidia-cudnn-cu12` (CTranslate2 ne les installe pas lui-même) : installer
  l'extra `clipper[cuda]` (pas de manipulation manuelle du PATH). Leurs
  dossiers `bin` sont ajoutés au PATH du processus par
  `clipper.gpu.ensure_cuda_dlls_on_path()`, appelée par `get_device()` —
  sinon CTranslate2 ne voit pas le GPU et `clipper.gpu` retombe sur CPU en
  silence (ce n'est pas un bug, juste l'absence de CUDA détectée).
- Le modèle mediapipe (`blaze_face_short_range.tflite`) est **téléchargé au
  premier lancement** dans le cache utilisateur
  (`~/.cache/clipper/`, donc `%USERPROFILE%\.cache\clipper\` sous Windows) :
  premier `run` plus lent, et il faut le réseau une fois.
- `ank done` doit avoir `.venv\Scripts` en tête du PATH pour trouver `ank`
  et les outils de test qu'il lance (voir *Règles ank* ci-dessus).

## Orchestration (session orchestrateur)

Avant d'orchestrer des workers, lire `research/BONNES-PRATIQUES.md`
(local, ignoré par git : `E:\ClaudeRandom\TiktokParseUpload\research\BONNES-PRATIQUES.md`) :
délégation aux workers herdr (sonnet par défaut), quota Max, RAM du PC,
rangement, tests réels. Un worker dans `.worktrees/` n'a pas besoin de ce fichier.
