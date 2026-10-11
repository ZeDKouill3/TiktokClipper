from __future__ import annotations

import glob
import json
import logging
import os
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yt_dlp

from clipper import browser
from clipper.channel import atomic_write_json
from clipper.workspace import _YOUTUBE_HOSTS, DownloadError, extract_video_id  # réexport : même nom public

log = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, object] = {
    "cookies_file": None,
    "cookies_from_browser": None,
    # Compte (state/browser/<compte>/) dont les cookies YouTube sont exportes vers un
    # cookies.txt pour yt-dlp ; reglé, il remplace cookies_from_browser (SPEC-9225 R8).
    "cookies_profile": "",
    "js_runtimes": "node",
    # Coupures reseau (WinError 10054 sur usher.ttvnw.net...) : essais rapproches avant d'echouer.
    "network_retries": 15,
    "network_retry_pause_s": 5,
    # Fragments HLS/DASH telecharges en parallele par yt-dlp (entier >= 1) : un par un, une VOD
    # Twitch de 11-21 Go reste a 13-20 Mo/s.
    "concurrent_fragments": 8,
    # Reprises d'un fragment HLS/DASH en echec avant d'abandonner : un fragment perdu fait ensuite
    # echouer le telechargement (jamais de video a trous, ADR-ad2e).
    "fragment_retries": 20,
    # Binaire ffmpeg du remux d'un mp4 fragmente (resolu par le PATH).
    "ffmpeg_bin": "ffmpeg",
    # Binaire ffprobe qui mesure la duree reelle du fichier telecharge (resolu par le PATH).
    "ffprobe_bin": "ffprobe",
}

# Ecart tolere entre la duree annoncee par la plateforme et la duree reelle du fichier :
# le plus grand de _DURATION_TOLERANCE_S secondes et de _DURATION_TOLERANCE_RATIO de la duree annoncee.
_DURATION_TOLERANCE_S = 5.0
_DURATION_TOLERANCE_RATIO = 0.01

# Marqueurs d'une erreur de transport reseau (connexion fermee/coupee), cherches dans le message
# de l'erreur et de ses causes. Rien d'autre n'est reessaye ici (abonnes, prive, format...).
_NETWORK_MARKERS = (
    "10054",
    "connection reset",
    "connection aborted",
    "forcibly closed",
    "failed to download m3u8 information",
)

# meilleure qualite jusqu'a 1080p, conteneur mp4 (ADR-b16b: sortie normalisee
# pour les etapes suivantes du pipeline).
_FORMAT = (
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
    "/best[height<=1080][ext=mp4]/best[height<=1080]"
)

def _build_meta(info: dict[str, Any], url: str) -> dict[str, Any]:
    return {
        "video_id": info["id"],
        # info["webpage_url"] est la source de verite (yt-dlp la renseigne
        # toujours) ; l'URL demandee est un repli honnete si un extracteur ne
        # la fournit pas, jamais une URL reconstruite a partir du video_id
        # (ADR-ad2e : celle-ci suppose YouTube, fausse pour une VOD Twitch).
        "webpage_url": info.get("webpage_url") or url,
        "title": info.get("title"),
        "description": info.get("description"),
        "duration": info.get("duration"),
        "channel": info.get("channel") or info.get("uploader"),
        "language": info.get("language"),
        "chapters": info.get("chapters") or [],
        "heatmap": info.get("heatmap") or [],
        "sponsorblock_segments": info.get("sponsorblock_chapters") or [],
        # URL de la miniature donnee par yt-dlp : la console l'affiche tant qu'aucune image n'est
        # extraite du mp4 local (None quand l'extracteur n'en fournit pas).
        "thumbnail": info.get("thumbnail"),
    }


def _format_speed(bytes_per_second: float | None) -> str:
    if not bytes_per_second:
        return "?"
    return f"{bytes_per_second / 1_000_000:.1f} Mo/s"


THUMBNAIL_FILE = "thumbnail.json"


def _save_thumbnail(video_dir: Path, url: str) -> None:
    """Garde l'URL de miniature des le debut du telechargement : si celui-ci echoue, meta.json n'existe pas
    mais la console a de quoi afficher une image (SPEC console, point 1 de TASK-c32b)."""
    path = video_dir / THUMBNAIL_FILE
    video_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"url": url})


def is_youtube_url(url: str) -> bool:
    """Vrai pour une URL YouTube (dont youtu.be) : sa miniature se deduit de l'identifiant."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host == "youtu.be" or host in _YOUTUBE_HOSTS


def fetch_thumbnail(
    url: str,
    workspace_dir: str | Path = "workspace",
    *,
    ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> str | None:
    """Recupere l'URL de miniature par les metadonnees yt-dlp, sans telecharger la video
    (skip_download), et l'ecrit dans workspace/<id>/thumbnail.json comme le fait le
    telechargement. Un thumbnail.json deja present n'est pas refait. Leve DownloadError
    si yt-dlp echoue ou ne donne aucune miniature : jamais une image inventee (ADR-ad2e)."""
    video_id = extract_video_id(url)
    video_dir = Path(workspace_dir) / video_id
    if (video_dir / THUMBNAIL_FILE).exists():
        return None
    opts: dict[str, Any] = {"skip_download": True, "quiet": True, "noprogress": True}
    try:
        with (ydl_factory or yt_dlp.YoutubeDL)(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise DownloadError(f"miniature de {url!r} : {exc}") from exc
    thumbnail = (info or {}).get("thumbnail")
    if not thumbnail:
        raise DownloadError(f"miniature de {url!r} : yt-dlp n'en donne aucune")
    _save_thumbnail(video_dir, thumbnail)
    return thumbnail


def _progress_hook(video_id: str, video_dir: Path | None = None) -> Callable[[dict[str, Any]], None]:
    """Journalise la progression du telechargement (pourcentage, debit) :
    DEBUG a chaque evenement yt-dlp, INFO au plus toutes les 30 s ou tous les
    10 % (done_criteria de TASK-8abc). Enregistre aussi, une fois, l'URL de
    miniature de la video quand yt-dlp la donne."""
    last: dict[str, float] = {"pct": 0.0, "t": time.monotonic()}
    saved = {"thumbnail": False}

    def hook(d: dict[str, Any]) -> None:
        if d.get("status") != "downloading":
            return
        thumbnail = (d.get("info_dict") or {}).get("thumbnail")
        if video_dir is not None and thumbnail and not saved["thumbnail"]:
            saved["thumbnail"] = True
            _save_thumbnail(video_dir, thumbnail)
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes")
        speed = _format_speed(d.get("speed"))
        if not total or downloaded is None:
            log.debug("%s : telechargement, %s octets telecharges (%s)", video_id, downloaded, speed)
            return
        pct = downloaded / total * 100
        log.debug("%s : telechargement %.1f%% (%s)", video_id, pct, speed)
        now = time.monotonic()
        if pct >= 100.0 - 1e-9 or pct - last["pct"] >= 10.0 or now - last["t"] >= 30.0:
            last["pct"], last["t"] = pct, now
            log.info("%s : telechargement %.1f%% (%s)", video_id, pct, speed)

    return hook


def _ydl_opts(
    video_dir: Path,
    cookies_file: str | Path | None,
    cookies_from_browser: str | None,
    js_runtimes: str | None,
    video_id: str,
    concurrent_fragments: int = 8,
    fragment_retries: int = 20,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "format": _FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": str(video_dir / "%(id)s.%(ext)s"),
        "postprocessors": [{"key": "SponsorBlock", "categories": ["all"]}],
        "quiet": True,
        "noprogress": True,
        "concurrent_fragment_downloads": concurrent_fragments,
        # Un fragment indisponible fait echouer le telechargement au lieu d'etre saute : sinon la
        # video a un trou de pts et transcript/audio decalent des clips (TASK-4880).
        "skip_unavailable_fragments": False,
        "fragment_retries": fragment_retries,
        "progress_hooks": [_progress_hook(video_id, video_dir)],
    }
    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if js_runtimes:
        # yt-dlp doit resoudre les challenges JS (nsig) pour signer les URLs
        # des formats video/audio ; sans, YouTube renvoie 403 au telechargement.
        opts["js_runtimes"] = {js_runtimes: {}}
    return opts


def _is_network_error(exc: BaseException) -> bool:
    """Vrai si l'erreur (ou l'une de ses causes) est une coupure de transport reseau."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ConnectionError):
            return True
        text = str(cur).lower()
        if any(marker in text for marker in _NETWORK_MARKERS):
            return True
        inner = getattr(cur, "exc_info", None)
        nxt = inner[1] if isinstance(inner, tuple) and len(inner) > 1 else None
        cur = cur.__cause__ or nxt or cur.__context__
    return False


def _extract_with_retries(
    url: str,
    opts: dict[str, Any],
    ydl_factory: Callable[[dict[str, Any]], Any],
    video_id: str,
    retries: int,
    pause_s: float,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """Telecharge ; sur coupure reseau, reessaie jusqu'a `retries` fois (pause `pause_s`),
    puis laisse remonter l'erreur d'origine telle quelle."""
    attempt = 0
    while True:
        try:
            with ydl_factory(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except Exception as exc:
            if attempt >= retries or not _is_network_error(exc):
                raise
            attempt += 1
            reason = " ".join(str(exc).split())[:120]
            log.info("%s : coupure reseau, essai %d/%d : %s", video_id, attempt, retries, reason)
            sleep(pause_s)


def is_fragmented_mp4(path: str | Path) -> bool:
    """Vrai si le mp4 contient au moins une boite `moof` au premier niveau (mp4 fragmente).
    Ne lit que les en-tetes des boites, en sautant leur contenu ; un fichier illisible comme
    mp4 (en-tete incoherent) n'est pas considere fragmente."""
    with open(path, "rb") as f:
        pos = 0
        size_total = os.fstat(f.fileno()).st_size
        while pos + 8 <= size_total:
            f.seek(pos)
            header = f.read(8)
            if len(header) < 8:
                return False
            size, kind = struct.unpack(">I4s", header)
            if kind == b"moof":
                return True
            if size == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    return False
                size = struct.unpack(">Q", ext)[0]
            elif size == 0:  # la boite va jusqu'a la fin du fichier
                return False
            if size < 8:
                return False
            pos += size
    return False


def _remux_if_fragmented(video_file: Path, video_id: str, ffmpeg_bin: str) -> None:
    """Remuxe sans reencodage un mp4 fragmente en mp4 indexe (sinon chaque seek ffmpeg lit tout le
    fichier). Echec : DownloadError, fichier temporaire supprime, original laisse en place."""
    if not is_fragmented_mp4(video_file):
        return
    tmp = video_file.with_name(f"{video_file.stem}.remux.mp4")
    started = time.monotonic()
    cmd = [ffmpeg_bin, "-v", "error", "-y", "-i", str(video_file), "-map", "0",
           "-c", "copy", "-movflags", "+faststart", str(tmp)]
    try:
        try:
            proc = subprocess.run(cmd, capture_output=True)
        except FileNotFoundError as exc:
            raise DownloadError(f"{video_id} : remux impossible, ffmpeg introuvable ({ffmpeg_bin})") from exc
        if proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip()[-300:]
            raise DownloadError(f"{video_id} : remux ffmpeg echoue (code {proc.returncode}) : {detail}")
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise DownloadError(f"{video_id} : remux ffmpeg a produit un fichier vide")
        size = tmp.stat().st_size
        os.replace(tmp, video_file)
    finally:
        tmp.unlink(missing_ok=True)
    log.info("%s : mp4 fragmente remuxe en mp4 indexe (%.2f Go, %.0f s)",
             video_id, size / 1e9, time.monotonic() - started)


def _probe_duration(video_file: Path, ffprobe_bin: str) -> float:
    """Duree reelle (secondes) du fichier selon ffprobe ; DownloadError si ffprobe est absent,
    echoue ou ne donne aucune duree (jamais une duree supposee, ADR-ad2e)."""
    cmd = [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video_file)]
    try:
        proc = subprocess.run(cmd, capture_output=True)
    except FileNotFoundError as exc:
        raise DownloadError(f"duree de {video_file.name} illisible, ffprobe introuvable ({ffprobe_bin})") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()[-300:]
        raise DownloadError(f"ffprobe a echoue sur {video_file.name} (code {proc.returncode}) : {detail}")
    try:
        return float((json.loads(proc.stdout or b"{}").get("format") or {})["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DownloadError(f"duree inconnue pour {video_file.name} : fichier illisible") from exc


def _check_file_present(video_file: Path, video_id: str) -> None:
    """DownloadError (fichier laisse en place pour la reprise) si le fichier attendu est absent,
    d'un autre conteneur que mp4, ou vide."""
    if not video_file.exists():
        others = sorted(
            p.name for p in video_file.parent.glob(f"{glob.escape(video_id)}.*")
            if ".part" not in p.name and not p.name.endswith((".ytdl", ".json"))
        )
        if others:
            raise DownloadError(
                f"{video_id} : conteneur inattendu {others[0]!r} (attendu {video_file.name}) : "
                "le format choisi par yt-dlp n'est pas un mp4"
            )
        raise DownloadError(f"{video_id} : fichier video absent apres le telechargement ({video_file.name})")
    if video_file.stat().st_size == 0:
        raise DownloadError(f"{video_id} : fichier video vide apres le telechargement")


def _check_duration(real: float, declared: Any, video_id: str) -> None:
    """DownloadError si la duree reelle s'ecarte de la duree annoncee de plus de max(5 s, 1 %).
    Sans duree annoncee, rien a comparer (journalise)."""
    if real <= 0:
        raise DownloadError(f"{video_id} : fichier video vide apres le telechargement (duree {real:g} s)")
    if not isinstance(declared, (int, float)) or isinstance(declared, bool) or declared <= 0:
        log.info("%s : duree annoncee absente, duree reelle %.0f s non comparee", video_id, real)
        return
    tolerance = max(_DURATION_TOLERANCE_S, _DURATION_TOLERANCE_RATIO * declared)
    if abs(real - declared) > tolerance:
        raise DownloadError(
            f"{video_id} : telechargement incomplet, {real:.0f} s dans le fichier pour {declared:.0f} s "
            f"annoncees (ecart tolere {tolerance:.0f} s) ; relance l'etape download"
        )


def _remove_ytdlp_leftovers(video_dir: Path, video_id: str) -> None:
    """Supprime les restes de yt-dlp (<id>.mp4.part*, <id>.mp4.ytdl) une fois le fichier verifie."""
    removed = freed = 0
    esc = glob.escape(video_id)
    for path in [*video_dir.glob(f"{esc}.mp4.part*"), video_dir / f"{video_id}.mp4.ytdl"]:
        if path.is_file():
            freed += path.stat().st_size
            path.unlink()
            removed += 1
    if removed:
        log.info("%s : %d reste(s) de yt-dlp supprime(s) (%.1f Mo)", video_id, removed, freed / 1e6)


def _discard_unusable_download(video_dir: Path, video_id: str) -> None:
    """Supprime <id>.mp4 refuse par un controle et les restes de yt-dlp, pour que la relance
    de l'etape retelecharge au lieu de re-mesurer le meme fichier."""
    (video_dir / f"{video_id}.mp4").unlink(missing_ok=True)
    _remove_ytdlp_leftovers(video_dir, video_id)
    log.warning("%s : fichier refuse supprime, la prochaine execution retelechargera", video_id)


def download(
    url: str,
    workspace_dir: str | Path = "workspace",
    *,
    cookies_file: str | Path | None = None,
    cookies_from_browser: str | None = None,
    cookies_profile: str | None = "",
    js_runtimes: str | None = "node",
    ydl_factory: Callable[[dict[str, Any]], Any] = yt_dlp.YoutubeDL,
    network_retries: int = 15,
    network_retry_pause_s: float = 5,
    sleep: Callable[[float], None] = time.sleep,
    ffmpeg_bin: str = "ffmpeg",
    concurrent_fragments: int = 8,
    fragment_retries: int = 20,
    ffprobe_bin: str = "ffprobe",
) -> dict[str, Any]:
    """Download a YouTube video and write its metadata (ADR-b16b: a step
    reads its inputs and writes workspace/<video_id>/ itself).

    A video already present (video file and meta.json both on disk) is not
    re-downloaded; the recorded meta.json is returned as-is instead.

    Le fichier produit est verifie avant meta.json : present, mp4, non vide, duree ffprobe a
    max(5 s, 1 %) de la duree annoncee (meta.json : `duration` = reelle, `declared_duration` =
    annoncee). Sinon DownloadError, meta.json non ecrit, fichier refuse supprime (yt-dlp ne
    reecrit pas un fichier existant) : la relance de l'etape retelecharge.
    """
    video_id = extract_video_id(url)
    if isinstance(concurrent_fragments, bool) or not isinstance(concurrent_fragments, int)             or concurrent_fragments < 1:
        raise DownloadError(
            f"[download] concurrent_fragments doit etre un entier >= 1 (recu {concurrent_fragments!r})"
        )
    video_dir = Path(workspace_dir) / video_id
    meta_file = video_dir / "meta.json"
    video_file = video_dir / f"{video_id}.mp4"

    if meta_file.exists() and video_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise DownloadError(
                f"[download] {meta_file} est illisible ({exc}) : refais l'etape "
                "download avec --force"
            ) from exc

    if cookies_profile:
        if cookies_file:
            raise DownloadError(
                "[download] cookies_profile et cookies_file sont tous deux regles : "
                "yt-dlp ne lit qu'un fichier de cookies, retire l'un des deux"
            )
        try:
            cookies_file = browser.export_cookies(cookies_profile)
        except browser.BrowserError as exc:
            raise DownloadError(f"cookies du profil {cookies_profile!r} : {exc}") from exc
        cookies_from_browser = None  # le profil prime sur la lecture des cookies d'un navigateur

    video_dir.mkdir(parents=True, exist_ok=True)
    opts = _ydl_opts(
        video_dir, cookies_file, cookies_from_browser, js_runtimes, video_id, concurrent_fragments,
        fragment_retries,
    )
    log.info("%s : telechargement avec %d fragments simultanes", video_id, concurrent_fragments)
    info = _extract_with_retries(
        url, opts, ydl_factory, video_id, network_retries, network_retry_pause_s, sleep
    )

    try:
        _check_file_present(video_file, video_id)
    except DownloadError:
        # yt-dlp ne reecrit jamais un fichier final existant (meme vide) : sans cette mise de cote,
        # la relance le declarerait « deja telecharge » et echouerait a l'identique.
        if video_file.exists():
            _discard_unusable_download(video_dir, video_id)
        raise
    _remux_if_fragmented(video_file, video_id, ffmpeg_bin)
    real_duration = _probe_duration(video_file, ffprobe_bin)
    try:
        _check_duration(real_duration, info.get("duration"), video_id)
    except DownloadError as exc:
        _discard_unusable_download(video_dir, video_id)
        raise DownloadError(f"{exc} ; fichier supprime, la relance retelechargera") from exc
    _remove_ytdlp_leftovers(video_dir, video_id)

    meta = _build_meta(info, url)
    meta["declared_duration"] = meta["duration"]
    meta["duration"] = real_duration
    atomic_write_json(meta_file, meta)
    return meta
