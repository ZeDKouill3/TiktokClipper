from __future__ import annotations

import argparse
import dataclasses
import importlib.resources
import json
import logging
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from clipper.config import ConfigError, load_config
from clipper.models import ModelsError

_PROGRESS_POLL_SECONDS = 0.1
_INIT_FILES = (("config.toml", "config.example.toml"), ("rubric.toml", "rubric.toml"))

# Indirection pour injection dans les tests (ADR-35b7 §1 : 'serve' lance le
# worker en sous-processus).
_popen = subprocess.Popen


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipper",
        description="Pipeline de clips verticaux sous-titres a partir de videos YouTube ou VOD Twitch longues",
    )
    parser.add_argument("--config", default=None, help="Fichier de configuration (defaut : config.toml)")
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Journal detaille des etapes (-v : progression ; -vv : detail)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Ecrit config.toml et rubric.toml (grille embarquee) dans le dossier courant")
    p.add_argument("--force", action="store_true", help="Ecrase config.toml/rubric.toml existants")

    p = sub.add_parser("doctor", help="Diagnostic avant un premier clip (ffmpeg, claude, modeles, GPU...)")
    p.add_argument("--json", action="store_true", help="Rapport en JSON plutot qu'en texte")

    p = sub.add_parser("models", help="Modeles locaux (mediapipe, whisper)")
    models_sub = p.add_subparsers(dest="models_command", required=True)
    models_sub.add_parser(
        "prefetch", help="Telecharge le modele mediapipe et le modele whisper configure dans leurs caches habituels"
    )

    p = sub.add_parser("run", help="Traite une video : jusqu'a la revue (review) ou jusqu'au bout (auto)")
    p.add_argument("url", help="URL YouTube ou VOD Twitch (twitch.tv/videos/<id>) de la video")
    p.add_argument("--force", action="store_true", help="Relance les etapes deja faites")
    p.add_argument("--force-step", action="append", dest="force_step", metavar="ETAPE",
                   help="Relance cette etape et les suivantes (repetable)")
    p.add_argument("--short-clips", action=argparse.BooleanOptionalAction, default=None, dest="short_clips",
                   help="Clips courts pour cette video (sinon : [moments] short_clips du style)")

    p = sub.add_parser("download", help="Telecharge seulement la video (etape download, prechargement du worker)")
    p.add_argument("url", help="URL YouTube ou VOD Twitch (twitch.tv/videos/<id>) de la video")

    p = sub.add_parser("render", help="Reprend une video apres la revue (ou apres un echec) jusqu'au bout")
    p.add_argument("video_id")
    p.add_argument("--force", action="store_true", help="Relance les etapes deja faites")
    p.add_argument("--force-step", action="append", dest="force_step", metavar="ETAPE",
                   help="Relance cette etape et les suivantes (repetable)")
    p.add_argument("--short-clips", action=argparse.BooleanOptionalAction, default=None, dest="short_clips",
                   help="Clips courts si l'etape moments est relancee (sinon : valeur du style)")

    p = sub.add_parser("decide", help="Enregistre la decision humaine sur un moment (mode review)")
    p.add_argument("video_id")
    p.add_argument("moment_id", type=int)
    p.add_argument("decision", choices=("accepted", "rejected", "adjusted"))
    p.add_argument("--start", type=float, help="Nouveau debut (s), decision adjusted")
    p.add_argument("--end", type=float, help="Nouvelle fin (s), decision adjusted")
    p.add_argument("--comment", help="Commentaire libre, journalise avec la decision")

    p = sub.add_parser("status", help="Affiche l'etat d'une video (JSON)")
    p.add_argument("video_id")

    p = sub.add_parser("queue", help="Reprend les videos en file d'attente dont l'heure est venue")
    p.add_argument("--watch", action="store_true", help="Tourne en boucle")
    p.add_argument("--interval", type=float, default=60.0, help="Secondes entre deux passages (--watch)")

    sub.add_parser("worker", help="Lance le worker seul (file state/queue.json, un enfant a la fois)")

    p = sub.add_parser("browser", help="Profils de navigateur par compte (connexion manuelle)")
    browser_sub = p.add_subparsers(dest="browser_command", required=True)
    p = browser_sub.add_parser(
        "login", help="Ouvre Chrome sur le profil du compte, page de connexion, et attend sa fermeture"
    )
    p.add_argument("account", help="Identifiant du compte (ecran Comptes de la console)")
    p.add_argument("--url", default=None, help="Page a ouvrir (defaut : [browser] login_url, TikTok)")

    p = sub.add_parser("serve", help="Lance l'interface web (FastAPI ; 127.0.0.1 par defaut, --host exige un jeton)")
    p.add_argument(
        "--host", default=None,
        help="Adresse d'ecoute (defaut : [web] host de config.toml) ; hors 127.0.0.1, [web] token est exige",
    )
    p.add_argument("--port", type=int, default=None, help="Port d'ecoute (defaut : [web] port de config.toml)")
    return parser


def _exit_code(state: dict) -> int:
    from clipper import pipeline

    if state["status"] in ("done", "awaiting_review"):
        return 0
    if state["status"] == "queued":
        return pipeline.EXIT_QUEUED
    return 1


def _report(state: dict) -> None:
    line = f"{state.get('video_id', '?')} : {state['status']}"
    if state.get("reason"):
        line += f" ({state['reason']})"
    if state["status"] == "awaiting_review":
        line += f" ; moments a decider : {state.get('awaiting')}"
    if state["status"] == "queued":
        line += f" ; re-essai a {state.get('retry_at')}"
    for clip in state.get("clips") or []:
        line += f"\n  {clip['clip_id']} : {'pret' if clip['ready'] else 'non pret'} (qa {clip['qa_status']})"
    print(line)


def _step_elapsed(step: dict) -> float:
    started, finished = step.get("started_at"), step.get("finished_at")
    if not started or not finished:
        return 0.0
    return (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()


def _announce_step(name: str, step: dict, seen_running: set, reported_done: set,
                   stale_snapshot: dict) -> None:
    baseline_step = stale_snapshot.get(name)
    if baseline_step is not None:
        if step == baseline_step:
            return  # etat identique a celui d'avant l'appel : pas encore reparti
        del stale_snapshot[name]  # a bouge depuis l'appel : traite normalement desormais

    status = step["status"]
    if status == "running":
        if name not in seen_running:
            seen_running.add(name)
            print(f"[{name}] démarrée")
    elif status in ("done", "failed") and name not in reported_done:
        if name not in seen_running:
            seen_running.add(name)
            print(f"[{name}] démarrée")
        reported_done.add(name)
        if status == "done":
            print(f"[{name}] terminée en {_step_elapsed(step):.1f} s")
        else:
            print(f"[{name}] échec : {step.get('reason')}")


def _baseline_progress(video_id: str, config, force: bool) -> tuple[set, set, dict]:
    """Etapes deja terminees avant cet appel (deja faites, cache) : elles ne
    refont pas de travail visible cette fois-ci et ne doivent pas etre
    annoncees. --force les relance toutes, donc rien n'est mis de cote.

    Une etape en echec n'est PAS mise de cote : elle repart pour de vrai et
    doit etre reannoncee ('demarree' puis 'terminee'/'echec'). Mais tant que
    son etat sur disque n'a pas encore bouge depuis cet instant (une lecture
    peut survenir juste avant que la relance ne la touche), son ancien statut
    'failed' ne doit pas etre pris pour une nouvelle annonce : stale_snapshot
    retient l'etat de depart de chaque etape non 'done' pour le detecter."""
    from clipper import pipeline

    if force:
        return set(), set(), {}
    try:
        state = pipeline.load_state(video_id, config=config)
    except pipeline.PipelineError:
        return set(), set(), {}
    seen_running: set = set()
    reported_done: set = set()
    stale_snapshot: dict = {}
    for name in pipeline.STEPS:
        step = state["steps"][name]
        if step["status"] == "done":
            seen_running.add(name)
            reported_done.add(name)
        else:
            stale_snapshot[name] = dict(step)
    return seen_running, reported_done, stale_snapshot


def _watch_progress(video_id: str, config, stop_event: threading.Event,
                    seen_running: set, reported_done: set, stale_snapshot: dict) -> None:
    from clipper import pipeline

    while not stop_event.is_set():
        try:
            state = pipeline.load_state(video_id, config=config)
        except pipeline.PipelineError:
            stop_event.wait(_PROGRESS_POLL_SECONDS)
            continue
        for name in pipeline.STEPS:
            _announce_step(name, state["steps"][name], seen_running, reported_done, stale_snapshot)
        stop_event.wait(_PROGRESS_POLL_SECONDS)


def _run_with_progress(action, video_id: str, config, force: bool) -> dict:
    """Execute ``action`` (pipeline.run ou pipeline.render) en affichant la
    progression au fil de l'eau, lue depuis l'etat expose par
    clipper.pipeline (pipeline.json), sans toucher aux etapes ni a
    pipeline.py."""
    from clipper import pipeline

    seen_running, reported_done, stale_snapshot = _baseline_progress(video_id, config, force)
    stop_event = threading.Event()
    watcher = threading.Thread(
        target=_watch_progress,
        args=(video_id, config, stop_event, seen_running, reported_done, stale_snapshot),
        daemon=True,
    )
    watcher.start()
    try:
        state = action()
    finally:
        stop_event.set()
        watcher.join(timeout=2.0)
    for name in pipeline.STEPS:
        step = state.get("steps", {}).get(name)
        if step is not None:
            _announce_step(name, step, seen_running, reported_done, stale_snapshot)
    return state


def _init(force: bool) -> int:
    """Ecrit config.toml et rubric.toml dans le dossier courant a partir de
    la grille et de l'exemple de config embarques dans le paquet (clipper
    /assets), pour une installation depuis la wheel sans checkout du depot.
    Refuse d'ecraser un fichier existant sans --force (erreur explicite,
    ADR-ad2e : jamais de repli silencieux)."""
    assets = importlib.resources.files("clipper").joinpath("assets")
    targets = [(Path(dest), asset) for dest, asset in _INIT_FILES]
    if not force:
        existing = [str(dest) for dest, _ in targets if dest.exists()]
        if existing:
            print(f"erreur : {', '.join(existing)} existe(nt) deja (--force pour ecraser)", file=sys.stderr)
            return 1
    for dest, asset in targets:
        dest.write_bytes(assets.joinpath(asset).read_bytes())
        print(f"{dest} ecrit")
    return 0


def _doctor(as_json: bool) -> int:
    """« clipper doctor » (SPEC-38f7 R7) : rapport de diagnostic dans le
    dossier courant, avant que config.toml ne soit charge pour les autres
    commandes (clipper/doctor.py fait sa propre lecture, pour rapporter un
    config.toml absent ou invalide comme un point du rapport plutot que de
    planter avant d'avoir pu diagnostiquer le reste)."""
    from clipper import doctor

    points = doctor.report()
    print(json.dumps(points, ensure_ascii=False, indent=2) if as_json else doctor.format_text(points))
    return doctor.exit_code(points)


def _whisper_prefetch_factory(name: str, local_files_only: bool) -> object:
    """Fabrique whisper reelle de « clipper models prefetch ». Sonde locale
    (local_files_only=True) : faster_whisper.utils.download_model directement,
    sans charger le modele en memoire (juste verifier le cache). Telechargement
    reel (local_files_only=False) : la meme fabrique que l'etape transcribe
    (clipper.transcribe._whisper_model, jamais recopiee), qui declenche le
    telechargement Hugging Face au chargement si le modele manque."""
    if local_files_only:
        from faster_whisper.utils import download_model

        return download_model(name, local_files_only=True)
    from clipper.gpu import get_device
    from clipper.transcribe import _whisper_model

    device = get_device()
    return _whisper_model(name, device.type, device.compute_type)


def _mediapipe_prefetch_factory(config, local_files_only: bool) -> object:
    """Fabrique mediapipe reelle de « clipper models prefetch » : reutilise
    clipper.reframe.ensure_mediapipe_model (jamais recopiee)."""
    from clipper import models, reframe

    if local_files_only:
        path = models.mediapipe_model_path(config)
        if not path.exists():
            raise FileNotFoundError(str(path))
        return path
    return reframe.ensure_mediapipe_model(config.section("reframe"))


def _models_prefetch(config) -> int:
    from clipper import models

    results = models.prefetch(
        config, _whisper_prefetch_factory, lambda local_files_only: _mediapipe_prefetch_factory(config, local_files_only)
    )
    for result in results:
        print(f"{result.name} : {'deja present' if result.already_present else 'telecharge'}")
    return 0


_VERBOSITY_LEVELS = {0: logging.WARNING, 1: logging.INFO}


def _process_kind(command: str) -> str:
    """serve et worker gardent leur propre tag ; toute autre commande (run,
    render, decide, status, queue, browser) partage « run » (TASK-8067) :
    le pid, ajoute par journal.install, distingue les instances concurrentes
    de la meme commande (ex. les sous-processus 'python -m clipper run ...'
    lances par le worker, ADR-35b7 §1)."""
    return command if command in ("serve", "worker") else "run"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console_level = _VERBOSITY_LEVELS.get(args.verbose, logging.DEBUG)
    logging.basicConfig(level=console_level, format="%(asctime)s %(levelname)s %(message)s")
    # Le journal global (TASK-8067) abaisse le niveau du logger racine pour
    # recevoir ses propres evenements independamment de la verbosite console ;
    # on fixe donc explicitement le niveau du handler console herite de
    # basicConfig (NOTSET par defaut) pour qu'il garde son seuil actuel.
    for handler in logging.getLogger().handlers:
        if handler.level == logging.NOTSET:
            handler.setLevel(console_level)

    if args.command == "init":
        return _init(args.force)
    if args.command == "doctor":
        return _doctor(args.json)

    from clipper import accounts, browser, download, journal, pipeline

    try:
        config = load_config(args.config, base="config.toml") if args.config is not None else load_config()
        journal.install(_process_kind(args.command), config)
        if args.command == "run":
            video_id = download.extract_video_id(args.url)
            state = _run_with_progress(
                lambda: pipeline.run(args.url, config=config, force=args.force, force_steps=args.force_step,
                                     short_clips=args.short_clips),
                video_id, config, args.force,
            )
        elif args.command == "download":
            pipeline.download_only(args.url, config=config)
            print(f"{download.extract_video_id(args.url)} : download termine")
            return 0
        elif args.command == "render":
            state = _run_with_progress(
                lambda: pipeline.render(args.video_id, config=config, force=args.force,
                                        force_steps=args.force_step, short_clips=args.short_clips),
                args.video_id, config, args.force,
            )
        elif args.command == "decide":
            pipeline.decide(args.video_id, args.moment_id, args.decision, start=args.start, end=args.end,
                            comment=args.comment, config=config)
            print(f"{args.video_id} : moment {args.moment_id} {args.decision}")
            return 0
        elif args.command == "status":
            print(json.dumps(pipeline.load_state(args.video_id, config=config), ensure_ascii=False, indent=2))
            return 0
        elif args.command == "worker":
            from clipper import worker as worker_mod

            try:
                worker_mod.Worker(config=config).loop()
            except worker_mod.WorkerError as exc:  # ex. « un worker tourne déjà (pid N) » (audit 10/10, A1)
                print(f"erreur : {exc}", file=sys.stderr)
                return 1
            return 0
        elif args.command == "models":
            if args.models_command == "prefetch":
                return _models_prefetch(config)
            raise AssertionError(f"sous-commande models inconnue : {args.models_command!r}")
        elif args.command == "browser":
            if args.account not in {a["id"] for a in accounts.list_accounts(config)}:
                raise browser.BrowserError(
                    f"compte inconnu : {args.account!r} (crée-le dans l'écran Comptes de la console)"
                )
            print(f"navigateur ouvert sur le profil de {args.account} : connecte-toi, puis ferme la fenêtre")
            browser.login(args.account, args.url, config=config)
            return 0
        elif args.command == "serve":
            import uvicorn

            from clipper.web import create_app

            web_cfg = config.section("web")
            port = args.port if args.port is not None else web_cfg["port"]
            host = str(args.host if args.host is not None else web_cfg["host"])
            if host != "127.0.0.1" and not web_cfg["token"]:
                raise ConfigError(
                    f"[web] token : un jeton est exige pour ecouter sur {host!r} (hors bouclage) ; "
                    'ajoute token = "..." dans la table [web] de config.toml (ADR-35b7 §5)'
                )
            if host != web_cfg["host"] or int(port) != int(web_cfg["port"]):
                # l'app voit l'hote et le port réellement écoutés (Réglages > Accès), pas ceux de config.toml
                sections = {**config._sections,
                            "web": {**config._sections.get("web", {}), "host": host, "port": int(port)}}
                config = dataclasses.replace(config, _sections=sections)
            # le worker enfant lit la meme config que le serveur (sinon il retomberait sur config.toml)
            config_args = ["--config", args.config] if args.config is not None else []
            worker_proc = _popen([sys.executable, "-m", "clipper", *config_args, "worker"])
            try:
                uvicorn.run(create_app(config=config), host=host, port=int(port))
            finally:
                worker_proc.terminate()
            return 0
        else:
            if args.watch:
                pipeline.watch_queue(config=config, interval=args.interval)
                return 0
            states = pipeline.process_queue(config=config)
            for state in states:
                _report(state)
            return max((_exit_code(s) for s in states), default=0)
    except (pipeline.PipelineError, ConfigError, download.DownloadError, browser.BrowserError,
            accounts.AccountsError, ModelsError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 1

    _report(state)
    return _exit_code(state)


if __name__ == "__main__":
    sys.exit(main())
