from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipper.download import _probe_duration as _REAL_PROBE  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.parametrize(
    "url,expected_id",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=43s", "dQw4w9WgXcQ"),
        ("https://youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=5", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ],
)
def test_extract_video_id_from_known_url_shapes(url, expected_id):
    from clipper.download import extract_video_id

    assert extract_video_id(url) == expected_id


def test_extract_video_id_raises_for_unrecognized_url():
    from clipper.download import DownloadError, extract_video_id

    with pytest.raises(DownloadError):
        extract_video_id("https://example.com/not-youtube")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.twitch.tv/videos/2887271276",
        "https://twitch.tv/videos/2887271276",
        "https://www.twitch.tv/videos/2887271276?t=01h02m03s",
        "https://www.twitch.tv/videos/2887271276/",
    ],
)
def test_extract_video_id_from_twitch_vod_url(url):
    from clipper.download import extract_video_id

    assert extract_video_id(url) == "v2887271276"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.twitch.tv/ma_chaine",  # chaine (ou live, meme forme d'URL)
        "https://www.twitch.tv/ma_chaine/clip/AwkwardHelplessSalamanderSwiftRage",  # clip
    ],
)
def test_extract_video_id_raises_for_non_vod_twitch_url(url):
    from clipper.download import DownloadError, extract_video_id

    with pytest.raises(DownloadError):
        extract_video_id(url)


def test_extract_video_id_does_not_collide_twitch_and_youtube_id_spaces():
    from clipper.download import extract_video_id

    twitch_id = extract_video_id("https://www.twitch.tv/videos/2887271276")
    youtube_id = extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert twitch_id != youtube_id
    assert twitch_id.startswith("v")  # prefixe yt-dlp : jamais un id YouTube nu


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


_LAST_DECLARED: dict = {"duration": None}


@pytest.fixture(autouse=True)
def _probe_follows_declared_duration(monkeypatch):
    """Les faux yt-dlp ecrivent des octets bidon : ffprobe est remplace par la duree annoncee de la
    derniere video (les tests de duree reelle remplacent la sonde a leur tour)."""
    _LAST_DECLARED["duration"] = None
    monkeypatch.setattr(
        "clipper.download._probe_duration", lambda path, ffprobe_bin: _LAST_DECLARED["duration"] or 10.0
    )


def _make_fake_ydl(info: dict, captured_opts: dict):
    """Stand-in for yt_dlp.YoutubeDL: records the opts it is built with and
    returns a pre-recorded info_dict instead of touching the network. Writes
    a dummy file at the resolved outtmpl path to simulate the download."""

    class FakeYoutubeDL:
        def __init__(self, opts):
            captured_opts.clear()
            captured_opts.update(opts)
            self._opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, url, download=True):
            if download:
                _LAST_DECLARED["duration"] = info.get("duration")
                video_path = Path(self._opts["outtmpl"] % {"id": info["id"], "ext": "mp4"})
                video_path.parent.mkdir(parents=True, exist_ok=True)
                video_path.write_bytes(b"fake video bytes")
            return info

    return FakeYoutubeDL


def test_download_writes_meta_json_with_full_fields(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    workspace_dir = isolated_cwd / "workspace"
    captured_opts: dict = {}
    ydl_factory = _make_fake_ydl(info, captured_opts)

    meta = download(
        f"https://www.youtube.com/watch?v={info['id']}",
        workspace_dir=workspace_dir,
        ydl_factory=ydl_factory,
    )

    expected = {
        "video_id": info["id"],
        "webpage_url": info["webpage_url"],
        "title": info["title"],
        "description": info["description"],
        "duration": info["duration"],
        "declared_duration": info["duration"],
        "channel": info["channel"],
        "language": info["language"],
        "chapters": info["chapters"],
        "heatmap": info["heatmap"],
        "sponsorblock_segments": info["sponsorblock_chapters"],
        "thumbnail": info.get("thumbnail"),
    }
    assert meta == expected

    meta_file = workspace_dir / info["id"] / "meta.json"
    assert json.loads(meta_file.read_text(encoding="utf-8")) == expected


def test_download_defaults_chapters_heatmap_sponsorblock_to_empty_list(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_minimal.json")
    workspace_dir = isolated_cwd / "workspace"
    captured_opts: dict = {}
    ydl_factory = _make_fake_ydl(info, captured_opts)

    meta = download(
        f"https://youtu.be/{info['id']}",
        workspace_dir=workspace_dir,
        ydl_factory=ydl_factory,
    )

    assert meta["chapters"] == []
    assert meta["heatmap"] == []
    assert meta["sponsorblock_segments"] == []
    assert meta["channel"] == info["uploader"]


def test_download_twitch_vod_writes_meta_with_real_webpage_url(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_twitch.json")
    workspace_dir = isolated_cwd / "workspace"
    captured_opts: dict = {}
    ydl_factory = _make_fake_ydl(info, captured_opts)

    meta = download(
        "https://www.twitch.tv/videos/2887271276",
        workspace_dir=workspace_dir,
        ydl_factory=ydl_factory,
    )

    assert meta["video_id"] == "v2887271276"
    assert meta["webpage_url"] == "https://www.twitch.tv/videos/2887271276"
    assert meta["channel"] == info["uploader"]


def test_download_falls_back_to_requested_url_when_info_lacks_webpage_url(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    del info["webpage_url"]
    workspace_dir = isolated_cwd / "workspace"
    captured_opts: dict = {}
    ydl_factory = _make_fake_ydl(info, captured_opts)
    requested_url = f"https://www.youtube.com/watch?v={info['id']}"

    meta = download(requested_url, workspace_dir=workspace_dir, ydl_factory=ydl_factory)

    assert meta["webpage_url"] == requested_url


def test_download_skips_when_video_and_meta_already_present(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    workspace_dir = isolated_cwd / "workspace"
    video_dir = workspace_dir / info["id"]
    video_dir.mkdir(parents=True)
    (video_dir / f"{info['id']}.mp4").write_bytes(b"already downloaded")
    existing_meta = {"video_id": info["id"], "title": "deja telecharge"}
    (video_dir / "meta.json").write_text(json.dumps(existing_meta), encoding="utf-8")

    def ydl_factory_that_must_not_be_called(opts):
        raise AssertionError("yt-dlp ne doit pas etre invoque : la video est deja presente")

    meta = download(
        f"https://youtu.be/{info['id']}",
        workspace_dir=workspace_dir,
        ydl_factory=ydl_factory_that_must_not_be_called,
    )

    assert meta == existing_meta


def test_download_redownloads_when_only_meta_present_without_video_file(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    workspace_dir = isolated_cwd / "workspace"
    video_dir = workspace_dir / info["id"]
    video_dir.mkdir(parents=True)
    (video_dir / "meta.json").write_text(json.dumps({"video_id": info["id"]}), encoding="utf-8")

    captured_opts: dict = {}
    ydl_factory = _make_fake_ydl(info, captured_opts)

    meta = download(
        f"https://youtu.be/{info['id']}",
        workspace_dir=workspace_dir,
        ydl_factory=ydl_factory,
    )

    assert meta["title"] == info["title"]
    assert captured_opts


def test_download_requests_mp4_up_to_1080p(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    workspace_dir = isolated_cwd / "workspace"
    captured_opts: dict = {}
    ydl_factory = _make_fake_ydl(info, captured_opts)

    download(
        f"https://youtu.be/{info['id']}",
        workspace_dir=workspace_dir,
        ydl_factory=ydl_factory,
    )

    assert captured_opts["merge_output_format"] == "mp4"
    assert "1080" in captured_opts["format"]
    assert "mp4" in captured_opts["format"]
    assert captured_opts["js_runtimes"] == {"node": {}}


def test_download_passes_configured_js_runtimes_to_ydl_opts(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    workspace_dir = isolated_cwd / "workspace"
    captured_opts: dict = {}
    ydl_factory = _make_fake_ydl(info, captured_opts)

    download(
        f"https://youtu.be/{info['id']}",
        workspace_dir=workspace_dir,
        js_runtimes="deno",
        ydl_factory=ydl_factory,
    )

    assert captured_opts["js_runtimes"] == {"deno": {}}


def test_download_omits_js_runtimes_when_disabled(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    workspace_dir = isolated_cwd / "workspace"
    captured_opts: dict = {}
    ydl_factory = _make_fake_ydl(info, captured_opts)

    download(
        f"https://youtu.be/{info['id']}",
        workspace_dir=workspace_dir,
        js_runtimes=None,
        ydl_factory=ydl_factory,
    )

    assert "js_runtimes" not in captured_opts


def test_config_defaults_declares_cookies_options():
    from clipper.download import CONFIG_DEFAULTS

    assert "cookies_file" in CONFIG_DEFAULTS
    assert "cookies_from_browser" in CONFIG_DEFAULTS


def test_config_defaults_declares_js_runtimes_option_defaulting_to_node():
    from clipper.download import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["js_runtimes"] == "node"


def test_download_passes_cookies_file_to_ydl_opts(isolated_cwd, tmp_path):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    workspace_dir = isolated_cwd / "workspace"
    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text("# netscape cookies\n", encoding="utf-8")
    captured_opts: dict = {}
    ydl_factory = _make_fake_ydl(info, captured_opts)

    download(
        f"https://youtu.be/{info['id']}",
        workspace_dir=workspace_dir,
        cookies_file=cookies_path,
        ydl_factory=ydl_factory,
    )

    assert captured_opts["cookiefile"] == str(cookies_path)


def test_download_passes_cookies_from_browser_to_ydl_opts(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    workspace_dir = isolated_cwd / "workspace"
    captured_opts: dict = {}
    ydl_factory = _make_fake_ydl(info, captured_opts)

    download(
        f"https://youtu.be/{info['id']}",
        workspace_dir=workspace_dir,
        cookies_from_browser="firefox",
        ydl_factory=ydl_factory,
    )

    assert captured_opts["cookiesfrombrowser"] == ("firefox",)


def test_config_section_download_resolves_via_clipper_config(isolated_cwd):
    from clipper.config import load_config

    (isolated_cwd / "config.toml").write_text(
        '[download]\ncookies_file = "cookies.txt"\n', encoding="utf-8"
    )

    config = load_config(isolated_cwd / "config.toml")

    assert config.section("download") == {
        "cookies_file": "cookies.txt",
        "cookies_from_browser": None,
        "cookies_profile": "",
        "js_runtimes": "node",
        "network_retries": 15,
        "network_retry_pause_s": 5,
        "concurrent_fragments": 8,
        "fragment_retries": 20,
        "ffmpeg_bin": "ffmpeg",
        "ffprobe_bin": "ffprobe",
    }


# --- TASK-e522 : [download] cookies_profile (SPEC-9225 R8) ---


def test_config_defaults_declares_cookies_profile_empty_by_default():
    from clipper.download import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["cookies_profile"] == ""


def test_download_without_cookies_profile_never_touches_the_browser(isolated_cwd, monkeypatch):
    from clipper import browser
    from clipper.download import download

    monkeypatch.setattr(browser, "export_cookies", lambda *a, **k: pytest.fail("export inattendu"))
    info = _load_fixture("info_dict_full.json")
    captured_opts: dict = {}

    download(f"https://youtu.be/{info['id']}", workspace_dir=isolated_cwd / "workspace",
             ydl_factory=_make_fake_ydl(info, captured_opts))

    assert "cookiefile" not in captured_opts


def test_download_with_cookies_profile_exports_and_passes_the_file_to_ydl(isolated_cwd, monkeypatch):
    from clipper import browser
    from clipper.download import download

    exported = isolated_cwd / "state" / "browser" / "yt01" / "cookies.txt"
    calls = []

    def fake_export(account, **kwargs):
        calls.append(account)
        return exported

    monkeypatch.setattr(browser, "export_cookies", fake_export)
    info = _load_fixture("info_dict_full.json")
    captured_opts: dict = {}

    download(f"https://youtu.be/{info['id']}", workspace_dir=isolated_cwd / "workspace",
             cookies_profile="yt01", ydl_factory=_make_fake_ydl(info, captured_opts))

    assert calls == ["yt01"]
    assert captured_opts["cookiefile"] == str(exported)


def test_cookies_profile_takes_priority_over_cookies_from_browser(isolated_cwd, monkeypatch):
    from clipper import browser
    from clipper.download import download

    exported = isolated_cwd / "cookies.txt"
    monkeypatch.setattr(browser, "export_cookies", lambda account, **k: exported)
    info = _load_fixture("info_dict_full.json")
    captured_opts: dict = {}

    download(f"https://youtu.be/{info['id']}", workspace_dir=isolated_cwd / "workspace",
             cookies_profile="yt01", cookies_from_browser="firefox",
             ydl_factory=_make_fake_ydl(info, captured_opts))

    assert captured_opts["cookiefile"] == str(exported)
    assert "cookiesfrombrowser" not in captured_opts


def test_cookies_profile_together_with_cookies_file_is_an_explicit_error(isolated_cwd, monkeypatch):
    from clipper import browser
    from clipper.download import DownloadError, download

    monkeypatch.setattr(browser, "export_cookies", lambda *a, **k: pytest.fail("export inattendu"))
    info = _load_fixture("info_dict_full.json")

    with pytest.raises(DownloadError, match="cookies_profile.*cookies_file"):
        download(f"https://youtu.be/{info['id']}", workspace_dir=isolated_cwd / "workspace",
                 cookies_profile="yt01", cookies_file=isolated_cwd / "c.txt",
                 ydl_factory=_make_fake_ydl(info, {}))


def test_cookies_profile_export_failure_is_a_download_error_not_a_silent_fallback(isolated_cwd, monkeypatch):
    from clipper import browser
    from clipper.download import DownloadError, download

    def boom(account, **kwargs):
        raise browser.BrowserError("profil absent pour le compte yt01")

    monkeypatch.setattr(browser, "export_cookies", boom)
    info = _load_fixture("info_dict_full.json")

    with pytest.raises(DownloadError, match="profil absent"):
        download(f"https://youtu.be/{info['id']}", workspace_dir=isolated_cwd / "workspace",
                 cookies_profile="yt01", cookies_from_browser="firefox",
                 ydl_factory=_make_fake_ydl(info, {}))


def test_cookies_profile_not_exported_when_video_already_downloaded(isolated_cwd, monkeypatch):
    from clipper import browser
    from clipper.download import download

    monkeypatch.setattr(browser, "export_cookies", lambda *a, **k: pytest.fail("export inattendu"))
    info = _load_fixture("info_dict_full.json")
    video_dir = isolated_cwd / "workspace" / info["id"]
    video_dir.mkdir(parents=True)
    (video_dir / f"{info['id']}.mp4").write_bytes(b"x")
    (video_dir / "meta.json").write_text("{}", encoding="utf-8")

    assert download(f"https://youtu.be/{info['id']}", workspace_dir=isolated_cwd / "workspace",
                    cookies_profile="yt01", ydl_factory=_make_fake_ydl(info, {})) == {}


# --------------------------------------------------------------------------
# TASK-c32b point 1 : miniature de la plateforme enregistree (YouTube, Twitch...)
# --------------------------------------------------------------------------

THUMB = "https://static-cdn.example.invalid/previews/v2887271276-preview.jpg"


def test_download_records_the_platform_thumbnail_in_meta_json(isolated_cwd):
    from clipper.download import download

    info = {**_load_fixture("info_dict_twitch.json"), "thumbnail": THUMB}
    workspace_dir = isolated_cwd / "workspace"

    meta = download("https://www.twitch.tv/videos/2887271276", workspace_dir=workspace_dir,
                    ydl_factory=_make_fake_ydl(info, {}))

    assert meta["thumbnail"] == THUMB
    assert json.loads((workspace_dir / "v2887271276" / "meta.json").read_text(encoding="utf-8"))["thumbnail"] == THUMB


def test_download_keeps_the_thumbnail_even_when_the_download_fails(isolated_cwd):
    """Echec au telechargement : meta.json n'existe pas, mais l'URL de miniature donnee par yt-dlp reste lisible."""
    from clipper.download import download

    info = {**_load_fixture("info_dict_twitch.json"), "thumbnail": THUMB}
    workspace_dir = isolated_cwd / "workspace"

    class Failing:
        def __init__(self, opts):
            self._opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, url, download=True):
            for hook in self._opts["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100, "info_dict": info})
            raise RuntimeError("HTTP 403")

    with pytest.raises(RuntimeError):
        download("https://www.twitch.tv/videos/2887271276", workspace_dir=workspace_dir, ydl_factory=Failing)

    video_dir = workspace_dir / "v2887271276"
    assert not (video_dir / "meta.json").exists()
    assert json.loads((video_dir / "thumbnail.json").read_text(encoding="utf-8")) == {"url": THUMB}


# --- TASK-7dc5 : fetch_thumbnail ---------------------------------------------

class _ThumbYDL:
    def __init__(self, info):
        self.info = info
        self.calls = []

    def __call__(self, opts):
        self.opts = opts
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        self.calls.append(download)
        return self.info


def test_fetch_thumbnail_writes_json_with_skip_download(tmp_path):
    from clipper.download import fetch_thumbnail

    ydl = _ThumbYDL({"thumbnail": "https://img.example.invalid/a.jpg"})
    url = fetch_thumbnail("https://www.twitch.tv/videos/55", tmp_path, ydl_factory=ydl)

    assert url == "https://img.example.invalid/a.jpg"
    assert ydl.calls == [False] and ydl.opts["skip_download"] is True
    saved = json.loads((tmp_path / "v55" / "thumbnail.json").read_text(encoding="utf-8"))
    assert saved == {"url": "https://img.example.invalid/a.jpg"}


def test_fetch_thumbnail_without_thumbnail_raises_and_writes_nothing(tmp_path):
    from clipper.download import DownloadError, fetch_thumbnail

    with pytest.raises(DownloadError):
        fetch_thumbnail("https://www.twitch.tv/videos/55", tmp_path, ydl_factory=_ThumbYDL({}))
    assert not (tmp_path / "v55" / "thumbnail.json").exists()


def test_is_youtube_url():
    from clipper.download import is_youtube_url

    assert is_youtube_url("https://youtu.be/AAAAAAAAAAA")
    assert is_youtube_url("https://www.youtube.com/watch?v=AAAAAAAAAAA")
    assert not is_youtube_url("https://www.twitch.tv/videos/55")


def _flaky_ydl(info: dict, errors: list, calls: list):
    """YoutubeDL factice : leve les erreurs de `errors` une par une, puis reussit."""
    ok = _make_fake_ydl(info, {})

    class Flaky(ok):
        def extract_info(self, url, download=True):
            calls.append(url)
            if errors:
                raise errors.pop(0)
            return super().extract_info(url, download)

    return Flaky


def test_network_retry_defaults_declared():
    from clipper.download import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["network_retries"] == 15
    assert CONFIG_DEFAULTS["network_retry_pause_s"] == 5


def test_download_retries_on_connection_reset_then_succeeds(isolated_cwd, caplog):
    import logging

    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    errors = [
        Exception("ERROR: Failed to download m3u8 information: [WinError 10054] Une connexion existante a ete fermee"),
        Exception("Connection reset by peer"),
    ]
    calls: list = []
    sleeps: list = []
    with caplog.at_level(logging.INFO, logger="clipper.download"):
        meta = download(
            f"https://www.youtube.com/watch?v={info['id']}",
            workspace_dir=isolated_cwd / "workspace",
            ydl_factory=_flaky_ydl(info, errors, calls),
            sleep=sleeps.append,
            network_retries=15,
            network_retry_pause_s=7,
        )
    assert meta["video_id"] == info["id"]
    assert len(calls) == 3
    assert sleeps == [7, 7]
    assert "essai 1/15" in caplog.text and "essai 2/15" in caplog.text


def test_download_network_retries_exhausted_raises_original_error(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    original = Exception("[WinError 10054] connexion fermee")
    errors = [original] * 10
    calls: list = []
    sleeps: list = []
    with pytest.raises(Exception) as excinfo:
        download(
            f"https://www.youtube.com/watch?v={info['id']}",
            workspace_dir=isolated_cwd / "workspace",
            ydl_factory=_flaky_ydl(info, errors, calls),
            sleep=sleeps.append,
            network_retries=3,
        )
    assert excinfo.value is original
    assert len(calls) == 4
    assert len(sleeps) == 3


@pytest.mark.parametrize(
    "message",
    [
        "This video is only available to subscribers",
        "Private video. Sign in if you've been granted access",
        "Requested format is not available",
    ],
)
def test_download_does_not_retry_other_errors(isolated_cwd, message):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    calls: list = []
    sleeps: list = []
    with pytest.raises(Exception, match=message[:20]):
        download(
            f"https://www.youtube.com/watch?v={info['id']}",
            workspace_dir=isolated_cwd / "workspace",
            ydl_factory=_flaky_ydl(info, [Exception(message)], calls),
            sleep=sleeps.append,
        )
    assert len(calls) == 1
    assert sleeps == []


# --- TASK-349a : remux d'un mp4 fragmente (VOD Twitch fMP4) -----------------------------------------

import shutil
import subprocess

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent du PATH")


def _make_mp4(path: Path, *, fragmented: bool, seconds: int = 2) -> None:
    movflags = "frag_keyframe+empty_moov" if fragmented else "+faststart"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x90:rate=10",
            "-f", "lavfi", "-i", f"sine=duration={seconds}",
            "-c:v", "libx264", "-g", "5", "-c:a", "aac", "-shortest",
            "-movflags", movflags, str(path),
        ],
        check=True,
    )


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


@needs_ffmpeg
def test_is_fragmented_detects_fmp4_and_not_a_normal_mp4(tmp_path):
    from clipper.download import is_fragmented_mp4

    frag, normal = tmp_path / "frag.mp4", tmp_path / "normal.mp4"
    _make_mp4(frag, fragmented=True)
    _make_mp4(normal, fragmented=False)

    assert is_fragmented_mp4(frag) is True
    assert is_fragmented_mp4(normal) is False


def test_is_fragmented_is_false_for_garbage_bytes(tmp_path):
    from clipper.download import is_fragmented_mp4

    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"fake video bytes")
    assert is_fragmented_mp4(junk) is False


def _fragmented_ydl(info: dict, make):
    class Ydl:
        def __init__(self, opts):
            self._opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, url, download=True):
            _LAST_DECLARED["duration"] = info.get("duration")
            path = Path(self._opts["outtmpl"] % {"id": info["id"], "ext": "mp4"})
            path.parent.mkdir(parents=True, exist_ok=True)
            make(path)
            return info

    return Ydl


@needs_ffmpeg
def test_download_remuxes_a_fragmented_mp4_keeping_duration(isolated_cwd, caplog):
    import logging

    from clipper.download import download, is_fragmented_mp4

    info = {**_load_fixture("info_dict_full.json"), "id": "v123"}
    factory = _fragmented_ydl(info, lambda p: _make_mp4(p, fragmented=True, seconds=3))
    with caplog.at_level(logging.INFO, logger="clipper.download"):
        download("https://www.twitch.tv/videos/123", workspace_dir=isolated_cwd / "ws", ydl_factory=factory)

    video = isolated_cwd / "ws" / "v123" / "v123.mp4"
    assert not is_fragmented_mp4(video)
    assert _duration(video) == pytest.approx(3.0, abs=0.3)
    assert (isolated_cwd / "ws" / "v123" / "meta.json").exists()
    assert not list((isolated_cwd / "ws" / "v123").glob("*.remux*"))
    assert any("remux" in r.getMessage().lower() for r in caplog.records)


def test_download_remux_failure_raises_and_keeps_original(isolated_cwd, monkeypatch):
    from clipper import download as dl

    info = {**_load_fixture("info_dict_full.json"), "id": "v123"}
    monkeypatch.setattr(dl, "is_fragmented_mp4", lambda p: True)
    monkeypatch.setattr(
        dl.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"boom"),
    )
    factory = _fragmented_ydl(info, lambda p: p.write_bytes(b"original"))

    with pytest.raises(dl.DownloadError, match="remux"):
        dl.download("https://www.twitch.tv/videos/123", workspace_dir=isolated_cwd / "ws", ydl_factory=factory)

    vdir = isolated_cwd / "ws" / "v123"
    assert (vdir / "v123.mp4").read_bytes() == b"original"
    assert not (vdir / "meta.json").exists()
    assert sorted(p.name for p in vdir.iterdir() if p.name != "thumbnail.json") == ["v123.mp4"]


def test_download_remux_with_ffmpeg_missing_raises(isolated_cwd, monkeypatch):
    from clipper import download as dl

    info = {**_load_fixture("info_dict_full.json"), "id": "v123"}
    monkeypatch.setattr(dl, "is_fragmented_mp4", lambda p: True)

    def boom(cmd, **kw):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(dl.subprocess, "run", boom)
    factory = _fragmented_ydl(info, lambda p: p.write_bytes(b"original"))

    with pytest.raises(dl.DownloadError, match="ffmpeg"):
        dl.download("https://www.twitch.tv/videos/123", workspace_dir=isolated_cwd / "ws", ydl_factory=factory)
    assert not (isolated_cwd / "ws" / "v123" / "meta.json").exists()


def test_download_remux_empty_output_raises(isolated_cwd, monkeypatch):
    from clipper import download as dl

    info = {**_load_fixture("info_dict_full.json"), "id": "v123"}
    monkeypatch.setattr(dl, "is_fragmented_mp4", lambda p: True)

    def fake_run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(dl.subprocess, "run", fake_run)
    factory = _fragmented_ydl(info, lambda p: p.write_bytes(b"original"))

    with pytest.raises(dl.DownloadError, match="vide"):
        dl.download("https://www.twitch.tv/videos/123", workspace_dir=isolated_cwd / "ws", ydl_factory=factory)
    vdir = isolated_cwd / "ws" / "v123"
    assert (vdir / "v123.mp4").read_bytes() == b"original"
    assert not (vdir / "meta.json").exists()
    assert sorted(p.name for p in vdir.iterdir() if p.name != "thumbnail.json") == ["v123.mp4"]


def test_download_normal_mp4_never_calls_ffmpeg(isolated_cwd, monkeypatch):
    from clipper import download as dl

    info = {**_load_fixture("info_dict_full.json"), "id": "v123"}
    calls: list = []
    monkeypatch.setattr(dl.subprocess, "run", lambda *a, **k: calls.append(a))
    factory = _fragmented_ydl(info, lambda p: p.write_bytes(b"fake video bytes"))

    dl.download("https://www.twitch.tv/videos/123", workspace_dir=isolated_cwd / "ws", ydl_factory=factory)
    assert calls == []


# -- fragments simultanes (TASK-6318) ----------------------------------------


def test_config_defaults_declares_concurrent_fragments_at_8():
    from clipper.download import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["concurrent_fragments"] == 8


def test_download_passes_default_8_concurrent_fragments_to_ydl_opts(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    captured_opts: dict = {}
    download(f"https://youtu.be/{info['id']}", workspace_dir=isolated_cwd / "workspace",
             ydl_factory=_make_fake_ydl(info, captured_opts))

    assert captured_opts["concurrent_fragment_downloads"] == 8


def test_download_passes_configured_concurrent_fragments_to_ydl_opts(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    captured_opts: dict = {}
    download(f"https://youtu.be/{info['id']}", workspace_dir=isolated_cwd / "workspace",
             concurrent_fragments=3, ydl_factory=_make_fake_ydl(info, captured_opts))

    assert captured_opts["concurrent_fragment_downloads"] == 3


@pytest.mark.parametrize("bad", [0, -2])
def test_download_refuses_concurrent_fragments_below_1(isolated_cwd, bad):
    from clipper.download import DownloadError, download

    info = _load_fixture("info_dict_full.json")
    captured_opts: dict = {}
    with pytest.raises(DownloadError, match="concurrent_fragments"):
        download(f"https://youtu.be/{info['id']}", workspace_dir=isolated_cwd / "workspace",
                 concurrent_fragments=bad, ydl_factory=_make_fake_ydl(info, captured_opts))
    assert captured_opts == {}


def test_download_logs_the_number_of_concurrent_fragments(isolated_cwd, caplog):
    import logging

    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    with caplog.at_level(logging.INFO, logger="clipper.download"):
        download(f"https://youtu.be/{info['id']}", workspace_dir=isolated_cwd / "workspace",
                 concurrent_fragments=5, ydl_factory=_make_fake_ydl(info, {}))

    assert "5 fragments simultanes" in caplog.text


# -- fragments indisponibles (TASK-4880) -------------------------------------


def test_download_opts_refuse_to_skip_unavailable_fragments(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    captured_opts: dict = {}
    download(f"https://youtu.be/{info['id']}", workspace_dir=isolated_cwd / "workspace",
             ydl_factory=_make_fake_ydl(info, captured_opts))

    assert captured_opts["skip_unavailable_fragments"] is False
    assert captured_opts["fragment_retries"] == 20


def test_download_passes_configured_fragment_retries_to_ydl_opts(isolated_cwd):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    captured_opts: dict = {}
    download(f"https://youtu.be/{info['id']}", workspace_dir=isolated_cwd / "workspace",
             fragment_retries=7, ydl_factory=_make_fake_ydl(info, captured_opts))

    assert captured_opts["fragment_retries"] == 7


def test_config_defaults_declares_fragment_retries_at_20():
    from clipper.download import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["fragment_retries"] == 20


def _failing_replace(*args, **kwargs):
    raise OSError("coupure simulee pendant le remplacement")


def test_download_interrupted_meta_write_leaves_no_meta_json(isolated_cwd, monkeypatch):
    from clipper.download import download

    info = _load_fixture("info_dict_full.json")
    workspace_dir = isolated_cwd / "workspace"
    ydl_factory = _make_fake_ydl(info, {})
    monkeypatch.setattr("os.replace", _failing_replace)

    with pytest.raises(OSError):
        download(
            f"https://youtu.be/{info['id']}", workspace_dir=workspace_dir, ydl_factory=ydl_factory
        )

    assert not (workspace_dir / info["id"] / "meta.json").exists()


def test_download_truncated_meta_json_raises_explicit_error_naming_file_and_force(isolated_cwd):
    from clipper.download import DownloadError, download

    info = _load_fixture("info_dict_full.json")
    workspace_dir = isolated_cwd / "workspace"
    video_dir = workspace_dir / info["id"]
    video_dir.mkdir(parents=True)
    (video_dir / f"{info['id']}.mp4").write_bytes(b"deja la")
    (video_dir / "meta.json").write_text('{"video_id": "ab', encoding="utf-8")

    with pytest.raises(DownloadError) as excinfo:
        download(f"https://youtu.be/{info['id']}", workspace_dir=workspace_dir)

    assert "meta.json" in str(excinfo.value)
    assert "--force" in str(excinfo.value)


def test_fetch_thumbnail_interrupted_write_leaves_no_thumbnail_json(tmp_path, monkeypatch):
    from clipper.download import fetch_thumbnail

    monkeypatch.setattr("os.replace", _failing_replace)

    with pytest.raises(OSError):
        fetch_thumbnail(
            "https://www.twitch.tv/videos/55", tmp_path,
            ydl_factory=_ThumbYDL({"thumbnail": "https://img.example.invalid/a.jpg"}),
        )

    assert not (tmp_path / "v55" / "thumbnail.json").exists()


# --- TASK-c44dcd91e731 (audit lot H) : fichier verifie, restes nettoyes ------------------------------


def _ydl_writing(info: dict, on_download):
    class Ydl:
        def __init__(self, opts):
            self._opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, url, download=True):
            on_download(Path(self._opts["outtmpl"]).parent)
            return info

    return Ydl


_URL = "https://www.youtube.com/watch?v=abcdefghijk"
_INFO = {"id": "abcdefghijk", "webpage_url": _URL, "title": "t", "duration": 3600}


def _probe_returns(monkeypatch, seconds: float) -> None:
    monkeypatch.setattr("clipper.download._probe_duration", lambda path, ffprobe_bin: seconds)


def test_download_refuses_a_file_far_shorter_than_announced_and_writes_no_meta(isolated_cwd, monkeypatch):
    from clipper.download import DownloadError, download

    _probe_returns(monkeypatch, 3.0)
    ydl = _ydl_writing(_INFO, lambda d: (d / "abcdefghijk.mp4").write_bytes(b"x" * 100))

    with pytest.raises(DownloadError, match="3600"):
        download(_URL, isolated_cwd / "workspace", ydl_factory=ydl)

    video_dir = isolated_cwd / "workspace" / "abcdefghijk"
    assert not (video_dir / "meta.json").exists()
    assert not (video_dir / "abcdefghijk.mp4").exists()  # ecarte : la relance retelecharge


def test_download_rerun_after_a_too_short_file_downloads_again_and_succeeds(isolated_cwd, monkeypatch):
    """Regression I2 : yt-dlp ne reecrit jamais un fichier final existant ('already downloaded') ;
    le fichier juge incomplet doit donc etre ecarte pour que la relance retelecharge."""
    from clipper.download import DownloadError, download

    durations = iter([3.0, 3600.0])  # 1er telechargement : trop court ; 2e : complet
    monkeypatch.setattr("clipper.download._probe_duration", lambda path, ffprobe_bin: next(durations))
    calls: list[str] = []

    class FakeYdl:
        """Comme le vrai yt-dlp : un fichier video final deja present n'est pas retelecharge."""

        def __init__(self, opts):
            self._opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, url, download=True):
            path = Path(self._opts["outtmpl"] % {"id": _INFO["id"], "ext": "mp4"})
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                calls.append("already downloaded")
            else:
                calls.append("downloaded")
                path.write_bytes(b"x" * 100)
            return _INFO

    video_dir = isolated_cwd / "workspace" / "abcdefghijk"
    with pytest.raises(DownloadError, match="incomplet") as excinfo:
        download(_URL, isolated_cwd / "workspace", ydl_factory=FakeYdl)
    assert "relance" in str(excinfo.value)
    assert not (video_dir / "meta.json").exists()

    meta = download(_URL, isolated_cwd / "workspace", ydl_factory=FakeYdl)

    assert calls == ["downloaded", "downloaded"]
    assert meta["duration"] == 3600.0
    assert (video_dir / "meta.json").exists()


def test_download_too_short_file_is_set_aside_with_its_leftovers(isolated_cwd, monkeypatch):
    from clipper.download import DownloadError, download

    _probe_returns(monkeypatch, 3.0)
    video_dir = isolated_cwd / "workspace" / "abcdefghijk"

    def write(d):
        (d / "abcdefghijk.mp4").write_bytes(b"x" * 100)
        (d / "abcdefghijk.mp4.part").write_bytes(b"y" * 10)
        (d / "abcdefghijk.mp4.ytdl").write_bytes(b"{}")

    with pytest.raises(DownloadError, match="incomplet"):
        download(_URL, isolated_cwd / "workspace", ydl_factory=_ydl_writing(_INFO, write))

    assert sorted(p.name for p in video_dir.iterdir()) == []


def test_download_refuses_an_empty_file(isolated_cwd, monkeypatch):
    from clipper.download import DownloadError, download

    _probe_returns(monkeypatch, 3600.0)
    ydl = _ydl_writing(_INFO, lambda d: (d / "abcdefghijk.mp4").write_bytes(b""))

    with pytest.raises(DownloadError, match="vide"):
        download(_URL, isolated_cwd / "workspace", ydl_factory=ydl)

    assert not (isolated_cwd / "workspace" / "abcdefghijk" / "meta.json").exists()


def test_download_accepts_a_gap_within_max_5s_or_1_percent(isolated_cwd, monkeypatch):
    from clipper.download import download

    _probe_returns(monkeypatch, 3570.0)  # 30 s d'ecart : sous 1 % de 3600
    ydl = _ydl_writing(_INFO, lambda d: (d / "abcdefghijk.mp4").write_bytes(b"x" * 100))

    meta = download(_URL, isolated_cwd / "workspace", ydl_factory=ydl)

    assert meta["duration"] == 3570.0
    assert meta["declared_duration"] == 3600


def test_download_records_the_real_duration_and_the_declared_one(isolated_cwd, monkeypatch):
    from clipper.download import download

    _probe_returns(monkeypatch, 62.5)
    ydl = _ydl_writing({**_INFO, "duration": 60}, lambda d: (d / "abcdefghijk.mp4").write_bytes(b"x" * 100))

    meta = download(_URL, isolated_cwd / "workspace", ydl_factory=ydl)

    assert (meta["duration"], meta["declared_duration"]) == (62.5, 60)
    on_disk = json.loads((isolated_cwd / "workspace" / "abcdefghijk" / "meta.json").read_text(encoding="utf-8"))
    assert on_disk["duration"] == 62.5 and on_disk["declared_duration"] == 60


def test_download_unreadable_file_is_an_error_not_a_pass(isolated_cwd, monkeypatch):
    from clipper.download import DownloadError, download

    def boom(path, ffprobe_bin):
        raise DownloadError("ffprobe illisible")

    monkeypatch.setattr("clipper.download._probe_duration", boom)
    ydl = _ydl_writing(_INFO, lambda d: (d / "abcdefghijk.mp4").write_bytes(b"x" * 100))

    with pytest.raises(DownloadError):
        download(_URL, isolated_cwd / "workspace", ydl_factory=ydl)

    assert not (isolated_cwd / "workspace" / "abcdefghijk" / "meta.json").exists()


def test_download_names_the_real_container_when_it_is_not_mp4(isolated_cwd):
    from clipper.download import DownloadError, download

    ydl = _ydl_writing(_INFO, lambda d: (d / "abcdefghijk.webm").write_bytes(b"x" * 100))

    with pytest.raises(DownloadError, match="webm"):
        download(_URL, isolated_cwd / "workspace", ydl_factory=ydl)


def test_download_without_any_output_file_is_a_download_error(isolated_cwd):
    from clipper.download import DownloadError, download

    with pytest.raises(DownloadError, match="absent"):
        download(_URL, isolated_cwd / "workspace", ydl_factory=_ydl_writing(_INFO, lambda d: None))


def test_download_removes_ytdlp_leftovers_after_success(isolated_cwd, monkeypatch):
    from clipper.download import download

    _probe_returns(monkeypatch, 3600.0)

    def write(d: Path) -> None:
        (d / "abcdefghijk.mp4").write_bytes(b"x" * 100)
        (d / "abcdefghijk.mp4.part-Frag7").write_bytes(b"x" * 10)
        (d / "abcdefghijk.mp4.part-Frag8.part").write_bytes(b"x" * 10)
        (d / "abcdefghijk.mp4.part").write_bytes(b"x" * 10)
        (d / "abcdefghijk.mp4.ytdl").write_text("{}")

    download(_URL, isolated_cwd / "workspace", ydl_factory=_ydl_writing(_INFO, write))

    names = sorted(p.name for p in (isolated_cwd / "workspace" / "abcdefghijk").iterdir())
    assert names == ["abcdefghijk.mp4", "meta.json"]


def test_config_defaults_declares_ffprobe_bin():
    from clipper.download import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["ffprobe_bin"] == "ffprobe"


@needs_ffmpeg
def test_real_ffprobe_reads_the_duration_and_refuses_garbage(tmp_path):
    from clipper.download import DownloadError

    good, junk = tmp_path / "good.mp4", tmp_path / "junk.mp4"
    _make_mp4(good, fragmented=False, seconds=3)
    junk.write_bytes(b"fake video bytes")

    assert _REAL_PROBE(good, "ffprobe") == pytest.approx(3.0, abs=0.5)
    with pytest.raises(DownloadError):
        _REAL_PROBE(junk, "ffprobe")
