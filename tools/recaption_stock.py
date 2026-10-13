"""Regenere caption + hashtags des clips prets, jamais publies, dont la legende cite le mauvais jeu
(TASK-ee48b6241ea4 ; etude des vues 13/10, Q5 : « Sur Dofus Touch, ... » sur une VOD AION).

Un clip est a corriger si son sidecar ``output/<video>/<clip>.json`` est ``ready``, sans ``tiktok_post`` ni
``youtube_post`` (ni ``edited_at`` : legende retouchee a la main) et si sa legende commence par « Sur X, »
(ou « X vu par ») avec X ni la categorie Twitch du chapitre qui contient le debut du clip ni dans le titre de la
VOD (meme fonction que la validation de ``clipper.captions`` : ``caption_wrong_game``).

Meme methode que ``tools/retitle_stock.py`` : la vraie fonction ``clipper.captions.run`` tourne dans un dossier
temporaire, puis SEULS ``caption`` et ``hashtags`` des clips a corriger sont recopies, dans ``captions.json`` ET
dans le sidecar du clip (la repartition et la publication lisent le sidecar), apres une sauvegarde horodatee.
Un clip publie n'est jamais touche : ``clipper/learning.py`` rattache un post a son clip par caption+hashtags.
La legende n'est pas dessinee dans la video : aucun rendu a relancer.

    python tools/recaption_stock.py --dry-run
    python tools/recaption_stock.py --workspace E:/.../workspace --output-dir E:/.../output --video v2895815199
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clipper import captions, channel as channel_mod
from clipper.config import Config, load_config

INPUTS = ("transcript.json", "moments.json", "parts.json", "meta.json")
FIELDS = ("caption", "hashtags")
DEFAULT_CHANNEL = "twitch-gaming"
WIDE_ERRORS = Exception  # tout échec (LLM, config, disque) est renommé en RecaptionError nommant la vidéo


class RecaptionError(Exception):
    pass


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def select_videos(workspace: str | Path, *, channel: str | None = DEFAULT_CHANNEL,
                  only: list[str] | None = None) -> list[str]:
    """Vidéos dont l'étape captions est faite (chaîne ``channel``, toutes si None). ``only`` limite ; un id
    hors stock est une erreur."""
    workspace = Path(workspace)
    found: list[str] = []
    for state_path in sorted(workspace.glob("*/pipeline.json")):
        state = _read_json(state_path)
        if channel is not None and state.get("channel") != channel:
            continue
        if state.get("steps", {}).get("captions", {}).get("status") != "done":
            continue
        if not (state_path.parent / "captions.json").exists():
            continue
        found.append(state_path.parent.name)
    if only:
        missing = [v for v in only if v not in found]
        if missing:
            raise RecaptionError(f"video(s) inconnue(s) ou hors stock ({channel or 'toutes chaines'}) : "
                                 f"{', '.join(missing)}")
        found = [v for v in found if v in only]
    return found


def _sidecar(output_dir: Path, video_id: str, clip_id: str) -> dict[str, Any] | None:
    path = output_dir / video_id / f"{clip_id}.json"
    if not path.exists():
        return None
    try:
        data = _read_json(path)
    except ValueError as exc:
        raise RecaptionError(f"sidecar illisible : {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise RecaptionError(f"sidecar illisible : {path} (objet JSON attendu)")
    return data


def find_wrong_game_clips(video_id: str, *, workspace: str | Path, output_dir: str | Path) -> list[dict[str, Any]]:
    """Clips prêts, jamais publiés, ni retouchés à la main, dont la légende du sidecar cite le mauvais jeu :
    ``[{"id", "start", "category", "game", "caption"}]``. Sans chapitres (YouTube) : aucun."""
    workspace, output_dir = Path(workspace), Path(output_dir)
    vdir = workspace / video_id
    meta_path = vdir / "meta.json"
    meta = _read_json(meta_path) if meta_path.exists() else {}
    video_title = str(meta.get("title") or "")
    found: list[dict[str, Any]] = []
    for clip in _read_json(vdir / "captions.json")["clips"]:
        sidecar = _sidecar(output_dir, video_id, clip["id"])
        if sidecar is None or sidecar.get("ready") is not True:
            continue
        if sidecar.get("tiktok_post") or sidecar.get("youtube_post") or sidecar.get("edited_at"):
            continue
        category = captions.chapter_category(meta, float(clip["start"]))
        caption = str(sidecar.get("caption") or "")
        game = captions.caption_wrong_game(caption, category, video_title)
        if game is not None:
            found.append({"id": clip["id"], "start": clip["start"], "category": category, "game": game,
                          "caption": caption})
    return found


def _generate(video_id: str, workspace: Path, config: Config) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="recaption-") as tmp:
        tmp_ws = Path(tmp)
        vtmp = tmp_ws / video_id
        vtmp.mkdir()
        for name in INPUTS:
            src = workspace / video_id / name
            if src.exists():
                shutil.copy2(src, vtmp / name)
        out = captions.run(video_id, tmp_ws, config=config, force=True)
        return _read_json(out)["clips"]


def assert_only_caption_fields_changed(before: list[dict[str, Any]], after: list[dict[str, Any]],
                                       allowed_ids: set[str]) -> None:
    """Mêmes clips dans le même ordre ; hors ``caption``/``hashtags`` des ``allowed_ids``, rien ne change."""
    if [c["id"] for c in before] != [c["id"] for c in after]:
        raise RecaptionError("l'ordre ou l'ensemble des clips a change")
    for old, new in zip(before, after):
        for key in sorted(set(old) | set(new)):
            if old["id"] in allowed_ids and key in FIELDS:
                continue
            if old.get(key, KeyError) != new.get(key, KeyError):
                raise RecaptionError(f"clip {old['id']} : le champ {key!r} a change ({old.get(key)!r} -> {new.get(key)!r})")


def recaption_video(video_id: str, *, workspace: str | Path, output_dir: str | Path, config: Config,
                    backup_root: str | Path, dry_run: bool = False) -> dict[str, Any]:
    """Corrige la légende des clips au mauvais jeu d'une vidéo. ``dry_run`` ne laisse rien sur le disque. Tout
    échec : RecaptionError qui nomme la vidéo, captions.json et sidecars réels intacts."""
    workspace, output_dir = Path(workspace), Path(output_dir)
    cap_path = workspace / video_id / "captions.json"
    try:
        if not cap_path.exists():
            raise RecaptionError("captions.json absent")
        targets = find_wrong_game_clips(video_id, workspace=workspace, output_dir=output_dir)
        report: dict[str, Any] = {"video_id": video_id, "patched": False, "backup": None, "clips": []}
        if not targets:
            return report  # rien à corriger : aucun appel LLM
        old_data = _read_json(cap_path)
        old_clips = old_data["clips"]
        ids = {t["id"] for t in targets}
        new_by_id = {c["id"]: c for c in _generate(video_id, workspace, config)}
        missing = sorted(ids - set(new_by_id))
        if missing:
            raise RecaptionError(f"clips absents de la regeneration : {missing}")
        patched = []
        for old in old_clips:
            if old["id"] in ids:
                new = new_by_id[old["id"]]
                if not str(new.get("caption") or "").strip() or not new.get("hashtags"):
                    raise RecaptionError(f"legende ou hashtags regeneres vides pour le clip {old['id']}")
                patched.append({**old, "caption": new["caption"], "hashtags": list(new["hashtags"])})
            else:
                patched.append(dict(old))
        assert_only_caption_fields_changed(old_clips, patched, ids)
        by_id = {c["id"]: c for c in patched}
        report["clips"] = [{"id": t["id"], "category": t["category"], "game": t["game"], "old": t["caption"],
                            "new": by_id[t["id"]]["caption"]} for t in targets]
        if dry_run:
            return report
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = Path(backup_root) / f"{video_id}-{stamp}"
        backup_dir.mkdir(parents=True)
        shutil.copy2(cap_path, backup_dir / "captions.json")
        for t in targets:
            shutil.copy2(output_dir / video_id / f"{t['id']}.json", backup_dir / f"{t['id']}.json")
        for t in targets:
            sc_path = output_dir / video_id / f"{t['id']}.json"
            sidecar = _read_json(sc_path)
            new = by_id[t["id"]]
            _write_json_atomic(sc_path, {**sidecar, "caption": new["caption"], "hashtags": new["hashtags"]})
        _write_json_atomic(cap_path, {**old_data, "clips": patched})
        assert_only_caption_fields_changed(old_clips, _read_json(cap_path)["clips"], ids)  # relecture disque
        report.update(patched=True, backup=str(backup_dir))
        return report
    except RecaptionError as exc:
        raise RecaptionError(f"{video_id} : {exc}") from exc
    except WIDE_ERRORS as exc:
        raise RecaptionError(f"{video_id} : {type(exc).__name__} : {exc}") from exc


def _markdown_report(reports: list[dict[str, Any]], errors: dict[str, str], *, dry_run: bool) -> str:
    lines = ["# Recaption du stock (legendes au mauvais jeu)", "",
             f"Mode : {'dry-run (rien ecrit)' if dry_run else 'patch reel'}.", "",
             "Seuls `caption` et `hashtags` changent (captions.json et sidecar) ; aucun rendu a relancer.", "",
             "| Video | Clips a corriger | Patchee |", "|---|---|---|"]
    for r in reports:
        lines.append(f"| {r['video_id']} | {len(r['clips'])} | {'oui' if r['patched'] else 'non'} |")
    for vid, msg in errors.items():
        lines.append(f"| {vid} | - | ERREUR : {msg} |")
    lines.append("")
    for r in reports:
        if not r["clips"]:
            continue
        lines += [f"## {r['video_id']}", "", "| Clip | Categorie | Jeu cite | Ancienne legende | Nouvelle legende |",
                  "|---|---|---|---|---|"]
        lines += [f"| {c['id']} | {c['category']} | {c['game']} | {c['old']} | {c['new']} |" for c in r["clips"]]
        if r["backup"]:
            lines += ["", f"Sauvegarde : `{r['backup']}`"]
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", help="dossier workspace (defaut : [workspace_dir] de config.toml)")
    ap.add_argument("--output-dir", help="dossier output des sidecars (defaut : [output_dir] de config.toml)")
    ap.add_argument("--config", default="config.toml", help="config de base (defaut : config.toml)")
    ap.add_argument("--channel", default=DEFAULT_CHANNEL, help="chaine du stock (defaut : twitch-gaming)")
    ap.add_argument("--video", action="append", dest="videos", metavar="ID", help="limiter a cette video (repetable)")
    ap.add_argument("--dry-run", action="store_true", help="affiche ancienne -> nouvelle legende, n'ecrit rien")
    ap.add_argument("--backup-dir", default="research/recaption-backup")
    ap.add_argument("--report", help="ecrit le rapport markdown dans ce fichier")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    base = load_config(args.config)
    workspace = Path(args.workspace) if args.workspace else Path(base.workspace_dir)
    output_dir = Path(args.output_dir) if args.output_dir else Path(base.output_dir)
    watch = base.section("watch")
    config, _ = channel_mod.load_channel(args.channel, presets_dir=watch["presets_dir"], base=args.config)

    videos = select_videos(workspace, channel=args.channel, only=args.videos)
    reports: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for vid in videos:
        try:
            r = recaption_video(vid, workspace=workspace, output_dir=output_dir, config=config,
                                backup_root=args.backup_dir, dry_run=args.dry_run)
        except RecaptionError as exc:
            errors[vid] = str(exc)
            print(f"ERREUR {exc}", file=sys.stderr)
            continue
        reports.append(r)
        if not r["clips"]:
            continue
        print(f"{vid} ({len(r['clips'])} clip(s) au mauvais jeu){' [patche]' if r['patched'] else ''}")
        for c in r["clips"]:
            print(f"  {c['id']} [{c['category']}] {c['old']}  ->  {c['new']}")
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_markdown_report(reports, errors, dry_run=args.dry_run), encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
