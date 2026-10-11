from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

# Purge (TASK-886a) : fichiers LOURDS d'un dossier workspace/<id>/, refaisables par une etape ;
# tout le reste (pipeline.json, meta.json, transcription, moments, captions, vignettes, plans de
# recadrage, sous-titres...) sert a l'affichage et reste.
SOURCE_SUFFIX = ".mp4"  # la video source telechargee : <id>.mp4
HEAVY_FILES = ("transcribe_audio.wav",)
HEAVY_DIRS = ("frames", "qa", "vision_resize_tmp", "render")
# Restes de yt-dlp (<id>.mp4.part, .part-Frag*, .ytdl) et du remux (<id>.remux.mp4) : motifs relatifs a <id>.
HEAVY_GLOBS = ("{id}.mp4.part*", "{id}.mp4.ytdl", "{id}.remux.mp4")
PURGE_MARKER = "purged.json"
# Publications qu'un clip de output/ attend encore (publish.UNFINISHED_STATUSES, sans importer d'etape).
_BLOCKING_STATUSES = ("approved", "scheduled", "failed")
_BUSY_STATUSES = ("running", "queued")


_YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com"}
_TWITCH_HOSTS = {"twitch.tv"}
_TWITCH_VOD_PATH = re.compile(r"/videos/(\d+)/?$")


class DownloadError(Exception):
    """The URL couldn't be resolved to a video_id, or yt-dlp failed."""


def extract_video_id(url: str) -> str:
    """Pull the video id out of a YouTube or Twitch URL.

    Handles youtube.com/watch?v=, youtu.be/, /shorts/ and /live/ forms
    (see TASK-4ca0's done_criteria), and Twitch VOD URLs
    (twitch.tv/videos/<chiffres>, see TASK-9290's done_criteria). The Twitch
    id is returned exactly as yt-dlp assigns it (prefixe 'v', ex.
    v2887271276) : jamais un id YouTube de 11 caracteres, pas de collision
    possible entre les deux espaces d'id.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[len("www.") :]

    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
        if video_id:
            return video_id

    if host in _YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v")
            if values:
                return values[0]
        for prefix in ("/shorts/", "/live/"):
            if parsed.path.startswith(prefix):
                video_id = parsed.path[len(prefix) :].strip("/").split("/")[0]
                if video_id:
                    return video_id

    if host in _TWITCH_HOSTS:
        match = _TWITCH_VOD_PATH.match(parsed.path)
        if match:
            return f"v{match.group(1)}"
        raise DownloadError(
            f"Twitch : seules les VOD (twitch.tv/videos/<id>) sont prises en charge, "
            f"pas les chaines, clips ou lives ({url!r})"
        )

    raise DownloadError(f"impossible d'extraire le video_id de {url!r}")


class PurgeRefused(Exception):
    """Purge impossible en l'etat (message francais, nomme la cause)."""


# --------------------------------------------------------------------------
# Purge des fichiers lourds (TASK-886a)
# --------------------------------------------------------------------------


def _size(path: Path) -> int:
    # os.scandir : une seule lecture de dossier, taille lue dans l'entrée (pas de stat séparé par fichier).
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    total = 0
    pending = [str(path)]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                elif entry.is_file():
                    total += entry.stat().st_size
    return total


def disk_usage(workspace_root: str | Path, output_root: str | Path) -> dict[str, int]:
    return {"workspace_bytes": _size(Path(workspace_root)), "output_bytes": _size(Path(output_root))}


def heavy_paths(video_id: str, root: str | Path = "workspace") -> list[Path]:
    video_dir = Path(root) / video_id
    candidates = [video_dir / f"{video_id}{SOURCE_SUFFIX}", *(video_dir / n for n in HEAVY_FILES),
                  *(video_dir / n for n in HEAVY_DIRS)]
    for pattern in HEAVY_GLOBS:
        candidates.extend(sorted(video_dir.glob(pattern.format(id=glob.escape(video_id)))))
    return [p for p in candidates if p.exists()]


def heavy_size(video_id: str, root: str | Path = "workspace") -> int:
    return sum(_size(p) for p in heavy_paths(video_id, root))


def is_purged(video_id: str, root: str | Path = "workspace") -> bool:
    """Vrai tant que la source a ete purgee et n'est pas revenue (retelechargee)."""
    video_dir = Path(root) / video_id
    return (video_dir / PURGE_MARKER).is_file() and not (video_dir / f"{video_id}{SOURCE_SUFFIX}").exists()


def _read_list(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PurgeRefused(f"fichier illisible ({path.name}) : {exc}") from exc
    if not isinstance(data, list):
        raise PurgeRefused(f"fichier illisible ({path.name}) : une liste est attendue")
    return data


def busy_reason(video_id: str, root: str | Path, queue_path: str | Path | None) -> str | None:
    """Pourquoi la video est en traitement ou en file (None si elle ne l'est pas)."""
    state_file = Path(root) / video_id / "pipeline.json"
    if state_file.is_file():
        try:
            status = json.loads(state_file.read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError) as exc:
            raise PurgeRefused(f"{video_id} : pipeline.json illisible : {exc}") from exc
        if status in _BUSY_STATUSES:
            return f"{video_id} est en cours de traitement ({status})"
    if queue_path is not None and Path(queue_path).is_file():
        if any(isinstance(e, dict) and e.get("video_id") == video_id for e in _read_list(Path(queue_path))):
            return f"{video_id} est dans la file de traitement"
    return None


def purge_heavy(video_id: str, root: str | Path = "workspace", *, queue_path: str | Path | None = None) -> int:
    """Supprime les fichiers lourds de workspace/<video_id>/ ; renvoie les octets liberes."""
    video_dir = Path(root) / video_id
    if not video_dir.is_dir():
        raise PurgeRefused(f"dossier de la video introuvable : {video_dir}")
    reason = busy_reason(video_id, root, queue_path)
    if reason:
        raise PurgeRefused(f"purge refusee : {reason}")
    freed = 0
    for path in heavy_paths(video_id, root):
        freed += _size(path)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    (video_dir / PURGE_MARKER).write_text(
        json.dumps({"purged_at": datetime.now(timezone.utc).isoformat(), "freed_bytes": freed}), encoding="utf-8")
    log.info("purge de %s : %d octets liberes", video_id, freed)
    return freed


def _blocking_publications(video_id: str, publish_dir: str | Path | None) -> list[str]:
    names: list[str] = []
    if publish_dir is None or not Path(publish_dir).is_dir():
        return names
    for path in sorted(Path(publish_dir).glob("*.json")):
        for entry in _read_list(path):
            if not isinstance(entry, dict) or entry.get("video_id") != video_id:
                continue
            if entry.get("status") in _BLOCKING_STATUSES or entry.get("in_progress_since"):
                names.append(str(entry.get("clip_id")))
    return names


def clips_size(video_id: str, output_root: str | Path = "output") -> int:
    return _size(Path(output_root) / video_id)


def check_clips_purgeable(video_id: str, publish_dir: str | Path | None) -> None:
    blocking = _blocking_publications(video_id, publish_dir)
    if blocking:
        raise PurgeRefused(
            f"clips de {video_id} gardes : le clip {blocking[0]} a une publication programmee, en cours ou en attente"
            + (f" (et {len(blocking) - 1} autre(s))" if len(blocking) > 1 else ""))


def purge_clips(video_id: str, output_root: str | Path = "output", publish_dir: str | Path | None = None) -> int:
    """Supprime output/<video_id>/ ; refuse si un clip a une publication programmee, en cours ou en attente."""
    check_clips_purgeable(video_id, publish_dir)
    out = Path(output_root) / video_id
    freed = _size(out)
    if out.is_dir():
        shutil.rmtree(out)
    log.info("purge des clips de %s : %d octets liberes", video_id, freed)
    return freed


# --------------------------------------------------------------------------
# Suppression de clips choisis (TASK-2322)
# --------------------------------------------------------------------------


def _clip_files(video_id: str, clip_id: str, output_root: str | Path) -> list[Path]:
    """Fichiers de ce clip seul : ``<clip_id>.<ext>`` (mp4, sidecar json, miniature...), jamais ``<clip_id>-p1.*``."""
    folder = Path(output_root) / video_id
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.name.startswith(f"{clip_id}."))


def series_clip_ids(video_id: str, clip_id: str, output_root: str | Path = "output") -> list[str]:
    """Tous les clip_id de la serie de ``clip_id`` (lui compris), par numero de partie ; ``[clip_id]`` hors serie.
    Meme regle que publish.series_clip_ids (sidecar part/parts_total, base = id sans ``-pN``)."""
    folder = Path(output_root) / video_id

    def info(cid: str) -> tuple[str | None, int]:
        path = folder / f"{cid}.json"
        try:
            sidecar = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PurgeRefused(f"clip {cid} introuvable ou sidecar illisible : {exc}") from exc
        part = sidecar.get("part")
        if part is None or (sidecar.get("parts_total") or 1) <= 1:
            return None, 0
        return (cid.rsplit("-p", 1)[0] if "-p" in cid else cid), part

    base, part = info(clip_id)
    if base is None:
        return [clip_id]
    members = [(part, clip_id)]
    for path in sorted(folder.glob("*.json")):
        if path.stem == clip_id:
            continue
        other_base, other_part = info(path.stem)
        if other_base == base:
            members.append((other_part, path.stem))
    return [cid for _, cid in sorted(members)]


def _clip_publication_state(video_id: str, clip_ids: list[str],
                            publish_dir: str | Path | None) -> tuple[dict[str, str], set[str]]:
    """(clip_id -> raison, clips publies) : les premiers ont une publication programmee, en cours ou en attente
    (jamais supprimables) ; les seconds sont deja publies (leur sidecar est garde pour les stats)."""
    reasons: dict[str, str] = {}
    published: set[str] = set()
    if publish_dir is None or not Path(publish_dir).is_dir():
        return reasons, published
    wanted = set(clip_ids)
    for path in sorted(Path(publish_dir).glob("*.json")):
        for entry in _read_list(path):
            if not isinstance(entry, dict) or entry.get("video_id") != video_id or entry.get("clip_id") not in wanted:
                continue
            if entry.get("in_progress_since"):
                reasons.setdefault(entry["clip_id"], "publication en cours")
            elif entry.get("status") in _BLOCKING_STATUSES:
                reasons.setdefault(entry["clip_id"], "publication programmee ou en attente")
            elif entry.get("status") == "published":
                published.add(entry["clip_id"])
    return reasons, published - set(reasons)


def delete_clips(video_id: str, clip_ids: list[str], output_root: str | Path = "output",
                 publish_dir: str | Path | None = None) -> dict:
    """Supprime des clips de output/<video_id>/, serie entiere comprise ; tout ou rien : ``PurgeRefused`` (nommant le
    clip) si un clip de l'ensemble est programme, en cours ou en attente. Un clip jamais publie est supprime
    entierement (``deleted``) ; un clip deja publie perd son .mp4 et ses annexes mais garde son sidecar .json
    (lien post -> clip, stats, jury) (``video_deleted``)."""
    expanded: list[str] = []
    for clip_id in clip_ids:
        for member in series_clip_ids(video_id, clip_id, output_root):
            if member not in expanded:
                expanded.append(member)
    blocked, published = _clip_publication_state(video_id, expanded, publish_dir)
    if blocked:
        first = next(c for c in expanded if c in blocked)
        raise PurgeRefused(f"clip {first} garde : {blocked[first]}"
                           + (f" (et {len(blocked) - 1} autre(s) de la serie)" if len(blocked) > 1 else ""))
    freed = 0
    deleted: list[str] = []
    video_deleted: list[str] = []
    for clip_id in expanded:
        keep_sidecar = clip_id in published
        for path in _clip_files(video_id, clip_id, output_root):
            if keep_sidecar and path.name == f"{clip_id}.json":
                continue
            freed += path.stat().st_size
            path.unlink()
        (video_deleted if keep_sidecar else deleted).append(clip_id)
    log.info("suppression de clips de %s : %s supprime(s), video seule supprimee pour %s, %d octets liberes",
             video_id, ", ".join(deleted) or "aucun", ", ".join(video_deleted) or "aucun", freed)
    return {"deleted": deleted, "video_deleted": video_deleted, "freed_bytes": freed}
