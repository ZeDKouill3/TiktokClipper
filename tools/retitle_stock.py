"""Retitre le stock de clips AION dans un style de titre (« promesse » par défaut) sans toucher au reste
(TASK-a21f409cf69d, SPEC-810f ; option --style : test A/B des titres, SPEC-6d1f).

Pourquoi pas ``clipper render --force-step captions`` : l'etape captions reecrit aussi ``caption`` et
``hashtags``, or ``clipper/learning.py`` rattache un post TikTok a son clip par caption+hashtags. Ici les
titres sont regeneres avec la vraie fonction ``clipper.captions.run`` dans un dossier temporaire, puis
SEUL ``screen_title`` est recopie dans le ``captions.json`` reel, clip par clip (meme ``id``), apres une
sauvegarde horodatee. Les rendus (reframe, subtitles, render, qa) restent a lancer : l'outil affiche la
commande, il ne lance rien.

    python tools/retitle_stock.py --workspace E:/.../workspace --dry-run
    python tools/retitle_stock.py --workspace E:/.../workspace --video v2895815199
    python tools/retitle_stock.py --workspace E:/.../workspace --video v2895815199 --style promesse_emoji

Option ``--max-chars N`` (etude des vues 13/10 : les titres de plus de 35 caracteres font moins de vues) : ne
retitre que les clips dont le titre d'ecran depasse N caracteres, dans le style du clip (``pipeline.json``
``title_style``, sinon ``screen_title_style`` du sidecar), avec ``[captions] screen_title_max_chars = N`` a la
generation ; jamais un clip publie (``tiktok_post``/``youtube_post`` dans son sidecar sous ``--output-dir``).

Le style choisi est aussi ecrit dans ``pipeline.json`` (``title_style``, seule cle touchee) pour que le rendu
l'enregistre dans le sidecar du clip : la repartition ne place un clip que sur le compte de son style.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clipper import captions, channel as channel_mod
from clipper.config import Config, load_config

INPUTS = ("transcript.json", "moments.json", "parts.json", "meta.json")
FIRST_RENDER_STEP = "reframe"  # captions déjà patché : reframe, subtitles, render, qa suffisent
DEFAULT_STYLE = "promesse"
DEFAULT_CHANNEL = "twitch-gaming"
DEFAULT_GAME = "aion"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
WIDE_ERRORS = Exception  # tout échec (LLM, config, disque) est renommé en RetitleError nommant la vidéo


class RetitleError(Exception):
    pass


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def render_command(video_id: str) -> str:
    return f"clipper render --force-step {FIRST_RENDER_STEP} {video_id}"


def api_call(video_id: str, *, base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
    """Appel de l'API du serve qui met le rendu en file du worker (jeton ``[web] token`` en en-tête)."""
    return {
        "method": "POST", "url": f"{base_url.rstrip('/')}/api/videos/{video_id}/retry",
        "headers": {"x-clipper-token": "<[web] token de config.toml>"},
        "json": {"from_step": FIRST_RENDER_STEP},
    }


def select_videos(workspace: str | Path, *, channel: str = DEFAULT_CHANNEL, game: str = DEFAULT_GAME,
                  only: list[str] | None = None) -> list[str]:
    """Vidéos du stock : chaîne ``channel``, captions faite, ``game`` (sans casse) dans le titre source ou
    dans le titre/la légende/les hashtags d'un clip. ``only`` limite ; un id hors stock est une erreur."""
    workspace = Path(workspace)
    needle = game.lower()
    found: list[str] = []
    for state_path in sorted(workspace.glob("*/pipeline.json")):
        vdir = state_path.parent
        state = _read_json(state_path)
        if state.get("channel") != channel:
            continue
        if state.get("steps", {}).get("captions", {}).get("status") != "done":
            continue
        cap_path = vdir / "captions.json"
        if not cap_path.exists():
            continue
        meta_path = vdir / "meta.json"
        texts = [str(_read_json(meta_path).get("title") or "")] if meta_path.exists() else []
        for clip in _read_json(cap_path)["clips"]:
            texts += [str(clip.get("title") or ""), str(clip.get("caption") or ""), *map(str, clip.get("hashtags") or [])]
        if needle in " ".join(texts).lower():
            found.append(vdir.name)
    if only:
        missing = [v for v in only if v not in found]
        if missing:
            raise RetitleError(f"video(s) inconnue(s) ou hors stock ({channel}/{game}) : {', '.join(missing)}")
        found = [v for v in found if v in only]
    return found


def merge_screen_titles(old_clips: list[dict[str, Any]], new_clips: list[dict[str, Any]],
                        only_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Copie de ``old_clips`` où seul ``screen_title`` vient de ``new_clips`` (même ``id``), pour les seuls
    ``only_ids`` si donné. Un id absent d'un côté ou l'autre, ou un titre vide : RetitleError."""
    new_by_id = {c["id"]: c for c in new_clips}
    old_ids = [c["id"] for c in old_clips]
    missing = [i for i in old_ids if i not in new_by_id]
    extra = [i for i in new_by_id if i not in old_ids]
    if missing or extra:
        raise RetitleError(
            f"clip_id incoherents entre captions.json reel et titres regeneres : absents des titres {missing}, "
            f"en trop {extra}"
        )
    patched = []
    for old in old_clips:
        if only_ids is not None and old["id"] not in only_ids:
            patched.append(dict(old))
            continue
        title = new_by_id[old["id"]].get("screen_title")
        if not isinstance(title, str) or not title.strip():
            raise RetitleError(f"screen_title regenere vide pour le clip {old['id']}")
        patched.append({**old, "screen_title": title})
    return patched


def assert_only_screen_title_changed(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> None:
    """Comparaison avant/après : mêmes clips dans le même ordre, tous les champs identiques sauf screen_title."""
    if [c["id"] for c in before] != [c["id"] for c in after]:
        raise RetitleError("l'ordre ou l'ensemble des clips a change")
    for old, new in zip(before, after):
        for key in sorted(set(old) | set(new)):
            if key != "screen_title" and old.get(key, KeyError) != new.get(key, KeyError):
                raise RetitleError(f"clip {old['id']} : le champ {key!r} a change ({old.get(key)!r} -> {new.get(key)!r})")


def _check_style(style: str) -> None:
    if style not in captions.SCREEN_TITLE_STYLES:
        raise RetitleError(f"style de titre inconnu {style!r} (valeurs permises : {', '.join(captions.SCREEN_TITLE_STYLES)})")


def _generate(video_id: str, workspace: Path, config: Config, style: str,
              max_chars: int | None = None) -> list[dict[str, Any]]:
    _check_style(style)
    # le style demande l'emporte sur celui du preset, le reste de [captions] est intact
    overrides: dict[str, Any] = {"screen_title_style": style}
    if max_chars is not None:
        overrides["screen_title_max_chars"] = max_chars
    sections = {**config._sections, "captions": {**config._sections.get("captions", {}), **overrides}}
    config = dataclass_replace(config, _sections=sections)
    with tempfile.TemporaryDirectory(prefix="retitle-") as tmp:
        tmp_ws = Path(tmp)
        vtmp = tmp_ws / video_id
        vtmp.mkdir()
        for name in INPUTS:
            src = workspace / video_id / name
            if src.exists():
                shutil.copy2(src, vtmp / name)
        out = captions.run(video_id, tmp_ws, config=config, force=True)
        return _read_json(out)["clips"]


def _read_sidecar(output_dir: Path, video_id: str, clip_id: str) -> dict[str, Any] | None:
    path = output_dir / video_id / f"{clip_id}.json"
    if not path.exists():
        return None
    try:
        data = _read_json(path)
    except ValueError as exc:
        raise RetitleError(f"sidecar illisible : {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise RetitleError(f"sidecar illisible : {path} (objet JSON attendu)")
    return data


def long_title_clips(video_id: str, old_clips: list[dict[str, Any]], output_dir: Path, max_chars: int,
                     state: dict[str, Any]) -> tuple[list[str], str]:
    """Ids des clips NON publiés dont le titre d'écran dépasse ``max_chars``, et le style de la vidéo
    (``pipeline.json`` ``title_style``, sinon style unique des sidecars de ces clips, sinon DEFAULT_STYLE)."""
    ids: list[str] = []
    styles: set[str] = set()
    for clip in old_clips:
        if len(str(clip.get("screen_title") or "")) <= max_chars:
            continue
        sidecar = _read_sidecar(output_dir, video_id, clip["id"]) or {}
        if sidecar.get("tiktok_post") or sidecar.get("youtube_post"):
            continue  # jamais un clip publié (learning.py rattache le post au clip)
        ids.append(clip["id"])
        if sidecar.get("screen_title_style"):
            styles.add(str(sidecar["screen_title_style"]))
    style = state.get("title_style")
    if not style:
        if len(styles) > 1:
            raise RetitleError(f"styles de titre differents selon les clips ({', '.join(sorted(styles))}) "
                               "et aucun title_style dans pipeline.json")
        style = next(iter(styles), DEFAULT_STYLE)
    return ids, style


def retitle_video(video_id: str, *, workspace: str | Path, config: Config, backup_root: str | Path,
                  dry_run: bool = False, style: str = DEFAULT_STYLE, max_chars: int | None = None,
                  output_dir: str | Path | None = None) -> dict[str, Any]:
    """Retitre une vidéo dans le style ``style``. ``dry_run`` ne laisse rien sur le disque. Avec ``max_chars``,
    ne retitre que les clips non publiés dont le titre dépasse N, dans leur style (``style`` est ignoré).
    Tout échec : RetitleError qui nomme la vidéo, captions.json réel intact."""
    workspace = Path(workspace)
    cap_path = workspace / video_id / "captions.json"
    state_path = workspace / video_id / "pipeline.json"
    try:
        only_ids: set[str] | None = None
        if max_chars is not None:
            if max_chars < 1:
                raise RetitleError(f"--max-chars doit etre un entier >= 1, recu {max_chars!r}")
            if output_dir is None:
                raise RetitleError("--max-chars a besoin du dossier output (sidecars) pour exclure les clips publies")
        else:
            _check_style(style)
        if not cap_path.exists():
            raise RetitleError("captions.json absent")
        old_data = _read_json(cap_path)
        old_clips = old_data["clips"]
        if max_chars is not None:
            ids, style = long_title_clips(video_id, old_clips, Path(output_dir), max_chars, _read_json(state_path))
            only_ids = set(ids)
            _check_style(style)
        report: dict[str, Any] = {
            "video_id": video_id, "style": style, "patched": False, "backup": None,
            "render_command": render_command(video_id), "clips": [],
        }
        if only_ids is not None and not only_ids:
            return report  # rien de plus long que N (ou tout est publie) : aucun appel LLM
        new_clips = _generate(video_id, workspace, config, style, max_chars)
        patched = merge_screen_titles(old_clips, new_clips, only_ids)
        assert_only_screen_title_changed(old_clips, patched)
        report["clips"] = [{"id": o["id"], "old": o["screen_title"], "new": p["screen_title"]}
                           for o, p in zip(old_clips, patched) if only_ids is None or o["id"] in only_ids]
        if dry_run:
            return report
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = Path(backup_root) / f"{video_id}-{stamp}"
        backup_dir.mkdir(parents=True)
        shutil.copy2(cap_path, backup_dir / "captions.json")
        shutil.copy2(state_path, backup_dir / "pipeline.json")
        state = _read_json(state_path)
        _write_json_atomic(cap_path, {**old_data, "clips": patched})
        _write_json_atomic(state_path, {**state, "title_style": style})
        assert_only_screen_title_changed(old_clips, _read_json(cap_path)["clips"])  # relecture disque
        report.update(patched=True, backup=str(backup_dir))
        return report
    except RetitleError as exc:
        raise RetitleError(f"{video_id} : {exc}") from exc
    except WIDE_ERRORS as exc:
        raise RetitleError(f"{video_id} : {type(exc).__name__} : {exc}") from exc


def _markdown_report(reports: list[dict[str, Any]], errors: dict[str, str], *, dry_run: bool, api_base: str) -> str:
    lines = ["# Retitrage du stock" + (f" en {reports[0]['style']}" if reports else ""), "",
             f"Mode : {'dry-run (rien ecrit)' if dry_run else 'patch reel'}.", "",
             "| Video | Clips | Patchee | Commande de rendu |", "|---|---|---|---|"]
    for r in reports:
        lines.append(f"| {r['video_id']} | {len(r['clips'])} | {'oui' if r['patched'] else 'non'} | `{r['render_command']}` |")
    for vid, msg in errors.items():
        lines.append(f"| {vid} | - | ERREUR | {msg} |")
    lines += ["", "Rendu via la file du worker (sans rien lancer ici) : "
              f"`POST {api_base}/api/videos/<id>/retry` avec `{{\"from_step\": \"{FIRST_RENDER_STEP}\"}}` "
              "et l'en-tete `x-clipper-token`.", ""]
    for r in reports:
        if r["patched"] or len(reports) == 1:
            lines += [f"## {r['video_id']}", "", "| Clip | Ancien | Nouveau |", "|---|---|---|"]
            lines += [f"| {c['id']} | {c['old']} | {c['new']} |" for c in r["clips"]]
            if r["backup"]:
                lines += ["", f"Sauvegarde : `{r['backup']}`"]
            lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", help="dossier workspace (defaut : [workspace_dir] de config.toml)")
    ap.add_argument("--config", default="config.toml", help="config de base (defaut : config.toml)")
    ap.add_argument("--channel", default=DEFAULT_CHANNEL)
    ap.add_argument("--game", default=DEFAULT_GAME, help="sous-chaine du titre/legende/hashtags (defaut : aion)")
    ap.add_argument("--video", action="append", dest="videos", metavar="ID", help="limiter a cette video (repetable)")
    ap.add_argument("--dry-run", action="store_true", help="affiche ancien -> nouveau titre, n'ecrit rien")
    ap.add_argument("--backup-dir", default="research/retitle-backup")
    ap.add_argument("--report", help="ecrit le rapport markdown dans ce fichier")
    ap.add_argument("--api-base", default=DEFAULT_BASE_URL)
    ap.add_argument("--style", choices=captions.SCREEN_TITLE_STYLES, default=DEFAULT_STYLE,
                    help="style de titre d'ecran du retitrage (defaut : promesse)")
    ap.add_argument("--max-chars", type=int, default=None, metavar="N",
                    help="ne retitre que les clips non publies dont le titre depasse N caracteres, dans leur style")
    ap.add_argument("--output-dir", help="dossier output des sidecars (defaut : [output_dir] de config.toml)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Les titres promesse_emoji contiennent des emojis : une console ou un fichier en cp1252 plantait l'affichage
    # apres le patch de la premiere video.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    base = load_config(args.config)
    workspace = Path(args.workspace) if args.workspace else Path(base.workspace_dir)
    output_dir = Path(args.output_dir) if args.output_dir else Path(base.output_dir)
    watch = base.section("watch")
    config, _ = channel_mod.load_channel(args.channel, presets_dir=watch["presets_dir"], base=args.config)

    videos = select_videos(workspace, channel=args.channel, game=args.game, only=args.videos)
    reports: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for vid in videos:
        try:
            r = retitle_video(vid, workspace=workspace, config=config, backup_root=args.backup_dir, dry_run=args.dry_run,
                             style=args.style, max_chars=args.max_chars, output_dir=output_dir)
        except RetitleError as exc:
            errors[vid] = str(exc)
            print(f"ERREUR {exc}", file=sys.stderr)
            continue
        reports.append(r)
        print(f"{vid} ({len(r['clips'])} clips){' [patche]' if r['patched'] else ''}")
        for c in r["clips"]:
            print(f"  {c['id']}: {c['old']}  ->  {c['new']}")
        print(f"  rendu : {r['render_command']}")
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_markdown_report(reports, errors, dry_run=args.dry_run, api_base=args.api_base), encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
