"""TASK-e522 : clipper.browser (SPEC-9225 R1, R8) et commande « clipper browser login ».

Playwright n'est jamais lance pour de vrai : un faux Playwright (objets
simules) remplace ``sync_playwright``. Aucun reseau, aucun navigateur.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from clipper import browser, network, __main__ as cli
from clipper.config import Config

ROOT = Path(__file__).resolve().parent.parent

NO_CHROME = (
    "BrowserType.launch_persistent_context: Chromium distribution 'chrome' is not found at "
    '/opt/google/chrome/chrome\nRun "playwright install chrome"'
)


class FakePage:
    def __init__(self):
        self.calls: list[tuple] = []

    def goto(self, url, **kwargs):
        self.calls.append(("goto", url))

    def __getattr__(self, name):  # fill/type/press... : enregistre, pour prouver qu'on n'y touche pas
        def call(*args, **kwargs):
            self.calls.append((name, args))

        return call


class FakeContext:
    def __init__(self, cookies=None):
        self.page = FakePage()
        self.pages = [self.page]
        self._cookies = cookies or []
        self.events: list[tuple] = []
        self.closed = False
        self.init_scripts: list[str] = []

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def cookies(self):
        return list(self._cookies)

    def wait_for_event(self, name, timeout=None):
        self.events.append((name, timeout))

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, context, error=None):
        self.context, self.error, self.launches = context, error, []

    def launch_persistent_context(self, user_data_dir, **kwargs):
        self.launches.append((str(user_data_dir), kwargs))
        if self.error:
            raise RuntimeError(self.error)
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        (Path(user_data_dir) / "Local State").write_text("{}", encoding="utf-8")
        return self.context


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium, self.stopped = chromium, False

    def stop(self):
        self.stopped = True


class FakeManager:
    def __init__(self, playwright):
        self.playwright = playwright

    def start(self):
        return self.playwright


def fake_playwright(monkeypatch, context=None, error=None):
    context = context or FakeContext()
    chromium = FakeChromium(context, error)
    pw = FakePlaywright(chromium)
    browser.use_playwright(lambda: FakeManager(pw))
    monkeypatch.setattr(browser, "_active", {})
    return context, chromium, pw


@pytest.fixture(autouse=True)
def _reset_override():
    network.reset()
    network.use_fetcher(lambda url: {"ip": "1.2.3.4", "city": "Paris", "country": "FR", "org": "AS1 Test"})  # jamais le réseau
    yield
    browser.use_playwright(None)
    network.use_fetcher(None)
    network.reset()


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ------------------------------------------------------------------ chemin du profil


def test_profile_dir_is_under_state_browser(cwd):
    assert browser.profile_dir("ab12cd") == Path("state/browser/ab12cd")


@pytest.mark.parametrize("bad", ["", "..", "../x", "a/b", "a\\b", ".hidden", "a b", "x" * 65, "/abs", "a\x00b", "é"])
def test_account_id_rejects_traversal_and_odd_ids(bad):
    with pytest.raises(browser.BrowserError, match="identifiant de compte invalide"):
        browser.profile_dir(bad)


def test_account_id_must_be_a_string():
    with pytest.raises(browser.BrowserError, match="identifiant de compte invalide"):
        browser.profile_dir(None)


def test_gitignore_covers_state_browser():
    out = subprocess.run(
        ["git", "check-ignore", "-q", "state/browser/ab12cd/cookies.txt"],
        cwd=ROOT, capture_output=True,
    )
    assert out.returncode == 0, "state/browser/ doit etre ignore par git"


def test_profile_status_absent_then_present(cwd):
    assert browser.profile_status("ab12cd") == {"present": False, "modified_at": None}
    d = browser.profile_dir("ab12cd")
    d.mkdir(parents=True)
    assert browser.profile_status("ab12cd")["present"] is False  # dossier vide = pas de profil
    (d / "Local State").write_text("{}", encoding="utf-8")
    status = browser.profile_status("ab12cd")
    assert status["present"] is True
    assert status["modified_at"].endswith("+00:00")


# ------------------------------------------------------------------ ouverture / connexion


class FakeProc:
    """Le Chrome normal lance en sous-processus : ``wait`` rend la main a la fermeture."""

    def __init__(self, release=None):
        self.release, self.waited = release, False

    def wait(self, timeout=None):
        if self.release is not None:
            self.release.wait(5)
        self.waited = True
        return 0


def fake_chrome(monkeypatch, tmp_path, proc=None):
    """Chrome present (``[browser] chrome_path`` -> faux binaire) ; Playwright interdit : la connexion
    ne doit jamais le toucher (TikTok refuse un Chrome pilote). Rend (liste des lancements, chemin, proc)."""
    exe = tmp_path / "chrome-fake"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    launches: list[tuple] = []
    proc = proc or FakeProc()

    def popen(args, **kwargs):
        launches.append((list(args), kwargs))
        return proc

    monkeypatch.setattr(browser, "_popen", popen)
    monkeypatch.setattr(browser, "_active", {})
    browser.use_playwright(lambda: pytest.fail("la connexion ne doit jamais passer par Playwright"))
    config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "o",
                    _sections={"browser": {"chrome_path": str(exe)}})
    return launches, config, proc


def test_login_launches_a_normal_chrome_subprocess_on_the_profile_and_waits_for_it_to_close(cwd, monkeypatch):
    launches, config, proc = fake_chrome(monkeypatch, cwd)

    browser.login("ab12cd", config=config)

    (args, _kwargs), = launches
    assert args[0] == config.section("browser")["chrome_path"]
    user_dir = [a for a in args if a.startswith("--user-data-dir=")]
    assert len(user_dir) == 1
    assert user_dir[0].split("=", 1)[1].replace("\\", "/").endswith("state/browser/ab12cd")
    assert "--no-first-run" in args
    assert args[-1] == "https://www.tiktok.com/login"  # [browser] login_url par defaut
    assert proc.waited is True  # attend la fermeture de la fenetre
    # un Chrome normal : aucun drapeau de pilotage (c'est ce que TikTok refuse a la connexion)
    assert not [a for a in args if "remote-debugging" in a or "enable-automation" in a or "headless" in a]


def test_login_accepts_another_url(cwd, monkeypatch):
    launches, config, _proc = fake_chrome(monkeypatch, cwd)

    browser.login("ab12cd", "https://accounts.google.com/ServiceLogin?service=youtube", config=config)

    assert launches[0][0][-1] == "https://accounts.google.com/ServiceLogin?service=youtube"


def test_login_never_uses_playwright_even_when_chrome_is_missing(cwd, monkeypatch):
    fake_chrome(monkeypatch, cwd)
    config = Config(mode="review", workspace_dir=cwd / "w", output_dir=cwd / "o",
                    _sections={"browser": {"chrome_path": str(cwd / "absent")}})

    with pytest.raises(browser.BrowserError):  # et le faux Playwright (pytest.fail) n'a pas ete touche
        browser.login("ab12cd", config=config)


def test_login_refuses_non_http_urls(cwd, monkeypatch):
    launches, config, _proc = fake_chrome(monkeypatch, cwd)
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "tiktok.com", ""):
        with pytest.raises(browser.BrowserError, match="URL"):
            browser.login("ab12cd", bad, config=config)
    assert launches == []


def test_missing_chrome_is_an_explicit_french_error_never_a_fallback(cwd, monkeypatch):
    launches, _config, _proc = fake_chrome(monkeypatch, cwd)
    monkeypatch.setattr(browser.shutil, "which", lambda name: None)
    monkeypatch.setattr(browser, "_chrome_candidates", lambda: [])

    with pytest.raises(browser.BrowserError) as err:
        browser.login("ab12cd")

    assert "Chrome est introuvable" in str(err.value) and "chrome_path" in str(err.value)
    assert launches == []


def test_a_configured_chrome_path_that_does_not_exist_is_an_explicit_error(cwd, monkeypatch):
    launches, _config, _proc = fake_chrome(monkeypatch, cwd)
    config = Config(mode="review", workspace_dir=cwd / "w", output_dir=cwd / "o",
                    _sections={"browser": {"chrome_path": str(cwd / "pas-chrome.exe")}})

    with pytest.raises(browser.BrowserError, match="pas-chrome.exe"):
        browser.login("ab12cd", config=config)
    assert launches == []


def test_chrome_is_found_on_the_path_when_no_chrome_path_is_set(cwd, monkeypatch):
    fake_chrome(monkeypatch, cwd)
    exe = cwd / "google-chrome"
    exe.write_text("x", encoding="utf-8")
    monkeypatch.setattr(browser.shutil, "which", lambda name: str(exe) if name == "google-chrome" else None)

    assert browser.find_chrome() == exe


def test_chrome_that_cannot_be_started_is_an_explicit_error(cwd, monkeypatch):
    _launches, config, _proc = fake_chrome(monkeypatch, cwd)

    def boom(args, **kwargs):
        raise OSError("accès refusé")

    monkeypatch.setattr(browser, "_popen", boom)
    with pytest.raises(browser.BrowserError, match="accès refusé"):
        browser.login("ab12cd", config=config)


def test_the_playwright_context_still_reports_a_missing_chrome_for_publishing(cwd, monkeypatch):
    fake_playwright(monkeypatch, error=NO_CHROME)

    with pytest.raises(browser.BrowserError) as err:
        with browser._open_context("ab12cd", headless=False):
            pass

    assert "Chrome" in str(err.value) and "playwright install chrome" in str(err.value)


def test_missing_playwright_is_an_explicit_error_with_install_command(cwd, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    browser.use_playwright(None)

    with pytest.raises(browser.BrowserError) as err:
        with browser._open_context("ab12cd", headless=False):
            pass

    assert "playwright" in str(err.value) and "uv pip install" in str(err.value)


def test_other_launch_error_is_reported_not_swallowed(cwd, monkeypatch):
    fake_playwright(monkeypatch, error="profil verrouille par un autre processus")

    with pytest.raises(browser.BrowserError, match="profil verrouille"):
        with browser._open_context("ab12cd", headless=False):
            pass


def test_start_login_returns_once_chrome_is_started_and_refuses_a_second_one(cwd, monkeypatch):
    release = threading.Event()
    _launches, config, proc = fake_chrome(monkeypatch, cwd, FakeProc(release))

    browser.start_login("ab12cd", config=config)
    with pytest.raises(browser.BrowserError, match="deja ouverte|déjà ouverte"):
        browser.start_login("ab12cd", config=config)
    release.set()
    for _ in range(100):
        if "ab12cd" not in browser._active:
            break
        threading.Event().wait(0.02)
    assert "ab12cd" not in browser._active
    assert proc.waited is True


def test_start_login_surfaces_a_missing_chrome(cwd, monkeypatch):
    fake_chrome(monkeypatch, cwd)
    config = Config(mode="review", workspace_dir=cwd / "w", output_dir=cwd / "o",
                    _sections={"browser": {"chrome_path": str(cwd / "absent")}})

    with pytest.raises(browser.BrowserError, match="Chrome"):
        browser.start_login("ab12cd", config=config)
    assert "ab12cd" not in browser._active


# ------------------------------------------------------------------ export des cookies (R8)

COOKIES = [
    {"name": "SID", "value": "abc", "domain": ".youtube.com", "path": "/", "expires": 1893456000.7,
     "httpOnly": True, "secure": True},
    {"name": "PREF", "value": "x=1", "domain": "www.youtube.com", "path": "/watch", "expires": -1,
     "httpOnly": False, "secure": False},
    {"name": "G", "value": "g", "domain": ".google.com", "path": "/", "expires": 1893456000,
     "httpOnly": False, "secure": True},
    {"name": "tt", "value": "tiktok", "domain": ".tiktok.com", "path": "/", "expires": 1893456000,
     "httpOnly": True, "secure": True},
]


def test_format_netscape_lines():
    text = browser.format_netscape(COOKIES[:2])
    lines = text.splitlines()
    assert lines[0] == "# Netscape HTTP Cookie File"
    assert "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tabc" in lines
    assert "www.youtube.com\tFALSE\t/watch\tFALSE\t0\tPREF\tx=1" in lines  # session : expires 0
    assert text.endswith("\n")


def test_format_netscape_rejects_tab_or_newline_in_a_value():
    bad = [{"name": "a", "value": "x\ty", "domain": ".youtube.com", "path": "/", "expires": 0}]
    with pytest.raises(browser.BrowserError, match="cookie"):
        browser.format_netscape(bad)


def test_export_cookies_writes_netscape_file_with_youtube_google_cookies_only(cwd, monkeypatch):
    context, chromium, pw = fake_playwright(monkeypatch, context=FakeContext(COOKIES))
    browser.profile_dir("yt01").mkdir(parents=True)
    (browser.profile_dir("yt01") / "Local State").write_text("{}", encoding="utf-8")

    path = browser.export_cookies("yt01")

    assert path == Path("state/browser/yt01/cookies.txt")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Netscape HTTP Cookie File\n")
    assert "SID" in text and "PREF" in text and "\tG\t" in text
    assert "tiktok" not in text  # les cookies TikTok ne servent pas a yt-dlp : ils ne sortent pas du profil
    assert chromium.launches[0][1]["channel"] == "chrome"
    assert context.closed and pw.stopped
    assert list(path.parent.glob("*.tmp")) == []


def test_export_cookies_without_profile_is_an_explicit_error(cwd, monkeypatch):
    fake_playwright(monkeypatch)

    with pytest.raises(browser.BrowserError, match="clipper browser login yt01"):
        browser.export_cookies("yt01")


def test_export_cookies_without_youtube_cookie_is_an_explicit_error(cwd, monkeypatch):
    fake_playwright(monkeypatch, context=FakeContext([COOKIES[3]]))
    browser.profile_dir("yt01").mkdir(parents=True)
    (browser.profile_dir("yt01") / "Local State").write_text("{}", encoding="utf-8")

    with pytest.raises(browser.BrowserError, match="aucun cookie"):
        browser.export_cookies("yt01")


def test_export_cookies_refused_while_login_window_is_open(cwd, monkeypatch):
    fake_playwright(monkeypatch)
    browser.profile_dir("yt01").mkdir(parents=True)
    (browser.profile_dir("yt01") / "Local State").write_text("{}", encoding="utf-8")
    browser._active["yt01"] = object()

    with pytest.raises(browser.BrowserError, match="fenêtre de connexion"):
        browser.export_cookies("yt01")


# ------------------------------------------------------------------ CLI


def test_parser_browser_login_defaults_and_url():
    parser = cli.build_parser()

    args = parser.parse_args(["browser", "login", "ab12cd"])
    assert (args.command, args.browser_command, args.account, args.url) == ("browser", "login", "ab12cd", None)

    args = parser.parse_args(["browser", "login", "ab12cd", "--url", "https://www.youtube.com"])
    assert args.url == "https://www.youtube.com"


def test_parser_browser_requires_subcommand(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["browser"])


def _config_file(tmp_path, accounts_json='{"accounts": [{"id": "ab12cd", "label": "Compte"}]}'):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "accounts.json").write_text(accounts_json, encoding="utf-8")
    cfg = tmp_path / "config.toml"
    cfg.write_text('mode = "review"\n', encoding="utf-8")
    return cfg


def test_main_browser_login_calls_browser_login(cwd, monkeypatch):
    cfg = _config_file(cwd)
    calls = []
    monkeypatch.setattr(browser, "login", lambda account, url=None, **kw: calls.append((account, url)))

    code = cli.main(["--config", str(cfg), "browser", "login", "ab12cd", "--url", "https://www.youtube.com"])

    assert code == 0
    assert calls == [("ab12cd", "https://www.youtube.com")]


def test_main_browser_login_unknown_account_is_an_error(cwd, monkeypatch, capsys):
    cfg = _config_file(cwd)
    monkeypatch.setattr(browser, "login", lambda *a, **k: pytest.fail("ne doit pas s'ouvrir"))

    code = cli.main(["--config", str(cfg), "browser", "login", "zz99"])

    assert code == 1
    assert "compte inconnu" in capsys.readouterr().err


def test_main_browser_login_reports_browser_error(cwd, monkeypatch, capsys):
    cfg = _config_file(cwd)

    def boom(*a, **k):
        raise browser.BrowserError("Chrome introuvable : playwright install chrome")

    monkeypatch.setattr(browser, "login", boom)

    code = cli.main(["--config", str(cfg), "browser", "login", "ab12cd"])

    assert code == 1
    assert "playwright install chrome" in capsys.readouterr().err


# ------------------------------------------------------------------ installation et documentation


def test_playwright_is_a_project_dependency():
    import tomllib

    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert "playwright" in deps


def test_setup_script_checks_playwright_and_chrome_with_a_clear_message():
    script = (ROOT / "tools" / "setup.ps1").read_text(encoding="utf-8")

    assert "import playwright.sync_api" in script
    assert "chrome.exe" in script and "Google Chrome introuvable" in script
    assert "playwright install chrome" in script


def test_readme_documents_tiktok_publishing_and_youtube_cookies():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tiktok = readme.split("## Publier sur TikTok", 1)[1].split("\n## ", 1)[0]
    cookies = readme.split("## Cookies YouTube", 1)[1].split("\n## ", 1)[0]

    assert "browser login" in tiktok
    assert "tiktok_account" not in tiktok and "créneaux" in tiktok   # le compte se choisit par publication (SPEC-6076 R2)
    assert "à la main" in tiktok and "jamais" in tiktok          # connexion manuelle, aucun identifiant saisi
    assert "Risques" in tiktok and "captcha" in tiktok.lower() and "arrêt" in tiktok
    assert "cookies_profile" in cookies and "cookies_from_browser" in cookies
    # connexion par un Chrome normal en sous-processus, jamais par Playwright (TASK-d90f)
    assert "Chrome normal" in tiktok and "chrome_path" in tiktok
    assert "refus" in tiktok.lower() or "refuse" in tiktok.lower()


def test_browser_section_resolves_through_clipper_config(tmp_path):
    from clipper.config import load_config

    (tmp_path / "config.toml").write_text('[browser]\nlogin_url = "https://exemple.invalid/login"\n', encoding="utf-8")

    section = load_config(tmp_path / "config.toml").section("browser")

    assert section["login_url"] == "https://exemple.invalid/login"
    assert section["cookie_domains"] == ["youtube.com", "google.com"]


# ------------------------------------------------------------------ connexion TikTok verifiee (SPEC-00d1 R2, R6)

import sqlite3  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def no_navigation(monkeypatch):
    """Lire la connexion ne lance ni Playwright ni Chrome : l'un ou l'autre ferait echouer le test."""
    def boom(*a, **k):
        raise AssertionError("navigateur lance pour lire la connexion")

    browser.use_playwright(boom)
    monkeypatch.setattr(browser, "_popen", boom)
    yield
    browser.use_cookie_reader(None)


def _profile(cwd, account="ab12cd"):
    directory = cwd / "state" / "browser" / account
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "Local State").write_text("{}", encoding="utf-8")
    return directory


def _cookie(name="sessionid", domain=".tiktok.com", expires=None):
    return {"name": name, "domain": domain, "expires": expires if expires is not None else (NOW + timedelta(days=30)).timestamp()}


def test_login_state_is_connected_with_the_expiry_date_when_a_live_session_cookie_exists(cwd, no_navigation):
    _profile(cwd)
    browser.use_cookie_reader(lambda account: [_cookie(expires=(NOW + timedelta(days=30)).timestamp())])

    state = browser.login_state("ab12cd", now=NOW)

    assert state == {"state": "connected", "checked_at": NOW.isoformat(timespec="seconds"),
                     "expires_at": (NOW + timedelta(days=30)).isoformat(timespec="seconds")}


def test_login_state_a_browser_session_cookie_without_expiry_is_connected(cwd, no_navigation):
    _profile(cwd)
    browser.use_cookie_reader(lambda account: [_cookie(expires=-1)])

    state = browser.login_state("ab12cd", now=NOW)

    assert state["state"] == "connected" and state["expires_at"] is None


def test_login_state_is_never_without_profile_and_does_not_even_read_cookies(cwd, no_navigation):
    browser.use_cookie_reader(lambda account: pytest.fail("cookies lus sans profil"))

    assert browser.login_state("ab12cd", now=NOW)["state"] == "never"


def test_login_state_is_never_when_the_profile_has_no_tiktok_session_cookie(cwd, no_navigation):
    _profile(cwd)
    browser.use_cookie_reader(lambda account: [
        _cookie(domain=".youtube.com"), _cookie(name="ttwid"), _cookie(domain="evil-tiktok.com.example")])

    assert browser.login_state("ab12cd", now=NOW)["state"] == "never"


def test_login_state_is_expired_when_every_session_cookie_is_past(cwd, no_navigation):
    _profile(cwd)
    browser.use_cookie_reader(lambda account: [
        _cookie(expires=(NOW - timedelta(days=1)).timestamp()), _cookie("sessionid_ss", expires=(NOW - timedelta(hours=1)).timestamp())])

    state = browser.login_state("ab12cd", now=NOW)

    assert state["state"] == "expired" and state["expires_at"] is None


def test_login_state_one_live_cookie_is_enough(cwd, no_navigation):
    _profile(cwd)
    browser.use_cookie_reader(lambda account: [
        _cookie(expires=(NOW - timedelta(days=1)).timestamp()), _cookie("sessionid_ss")])

    assert browser.login_state("ab12cd", now=NOW)["state"] == "connected"


def test_login_state_rejects_an_unsafe_account_id(cwd, no_navigation):
    with pytest.raises(browser.BrowserError, match="identifiant de compte invalide"):
        browser.login_state("../x")


def test_login_state_cookie_names_and_domains_are_settings(cwd, no_navigation):
    _profile(cwd)
    browser.use_cookie_reader(lambda account: [_cookie("autre", ".exemple.invalid")])
    config = Config(mode="review", workspace_dir=cwd / "w", output_dir=cwd / "o",
                    _sections={"browser": {"login_domains": ["exemple.invalid"], "login_cookies": ["autre"]}})

    assert browser.login_state("ab12cd", config=config, now=NOW)["state"] == "connected"
    assert browser.CONFIG_DEFAULTS["login_domains"] == ["tiktok.com"]
    assert "sessionid" in browser.CONFIG_DEFAULTS["login_cookies"]


def _chrome_cookie_db(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, "
               "expires_utc INTEGER, is_persistent INTEGER)")
    db.executemany("INSERT INTO cookies VALUES (?, ?, '', x'00', ?, ?)", rows)
    db.commit()
    db.close()


def _chrome_micros(moment):
    return int((moment.timestamp() + 11_644_473_600) * 1_000_000)


def test_the_default_reader_reads_the_chrome_cookie_database_without_values(cwd, no_navigation):
    directory = _profile(cwd)
    _chrome_cookie_db(directory / "Default" / "Network" / "Cookies", [
        (".tiktok.com", "sessionid", _chrome_micros(NOW + timedelta(days=10)), 1),
        (".tiktok.com", "ttwid", 0, 0),
        (".youtube.com", "SID", _chrome_micros(NOW + timedelta(days=10)), 1)])

    state = browser.login_state("ab12cd", now=NOW)
    cookies = browser._read_profile_cookies("ab12cd")

    assert state == {"state": "connected", "checked_at": NOW.isoformat(timespec="seconds"),
                     "expires_at": (NOW + timedelta(days=10)).isoformat(timespec="seconds")}
    assert all(set(c) == {"domain", "name", "expires"} for c in cookies)  # jamais une valeur
    assert next(c for c in cookies if c["name"] == "ttwid")["expires"] == -1


def test_the_default_reader_sees_an_expired_session_in_the_database(cwd, no_navigation):
    directory = _profile(cwd)
    _chrome_cookie_db(directory / "Default" / "Cookies", [
        (".tiktok.com", "sessionid", _chrome_micros(NOW - timedelta(days=2)), 1)])

    assert browser.login_state("ab12cd", now=NOW)["state"] == "expired"


def test_the_default_reader_without_a_cookie_file_means_never_connected(cwd, no_navigation):
    _profile(cwd)

    assert browser.login_state("ab12cd", now=NOW)["state"] == "never"


def test_a_locked_cookie_database_is_an_explicit_error_not_a_guess(cwd, no_navigation, monkeypatch):
    directory = _profile(cwd)
    _chrome_cookie_db(directory / "Default" / "Network" / "Cookies", [])

    def locked(src, dst):
        raise PermissionError("verrouillé")

    monkeypatch.setattr(browser.shutil, "copyfile", locked)
    with pytest.raises(browser.BrowserError, match="ferme la fenêtre Chrome"):
        browser.login_state("ab12cd", now=NOW)


def test_a_corrupt_cookie_database_is_an_explicit_error(cwd, no_navigation):
    directory = _profile(cwd)
    bad = directory / "Default" / "Network" / "Cookies"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"pas une base sqlite" * 50)

    with pytest.raises(browser.BrowserError, match="illisible"):
        browser.login_state("ab12cd", now=NOW)


def test_start_login_calls_on_close_once_the_chrome_window_is_closed(cwd, monkeypatch):
    release = threading.Event()
    _launches, config, _proc = fake_chrome(monkeypatch, cwd, FakeProc(release))
    closed = threading.Event()
    seen = []

    def on_close():
        seen.append("ab12cd" in browser._active)  # le profil est deja libere
        closed.set()

    browser.start_login("ab12cd", config=config, on_close=on_close)
    assert not closed.is_set()  # la fenetre est encore ouverte
    release.set()
    assert closed.wait(5) and seen == [False]


# ------------------------------------------------------------------ verrou de pilotage inter-processus
# (TASK-2456, revue r-publication I2) : le worker et le serveur web sont deux processus ; deux vrais
# processus Python, faux Playwright, aucun navigateur.

_PILOT_SCRIPT = r'''
import json, sys, time
from pathlib import Path
from clipper import browser

class Context:
    pages = []
    def add_init_script(self, script):
        pass
    def close(self):
        pass

class Chromium:
    def launch_persistent_context(self, user_data_dir, **kwargs):
        return Context()

class Playwright:
    chromium = Chromium()
    def stop(self):
        pass

class Manager:
    def start(self):
        return Playwright()

browser.use_playwright(lambda: Manager())
# Processus enfant : la fixture de conftest n'y est pas, on simule aussi l'IP française (jamais le vrai service).
from clipper import network
network.use_fetcher(lambda url: {"ip": "192.0.2.1", "country": "FR", "city": "Test", "org": "test"})
account, out, hold = sys.argv[1], Path(sys.argv[2]), float(sys.argv[3])
try:
    with browser._open_context(account, headless=True):
        start = time.time()
        Path(str(out) + ".inside").write_text("1", encoding="utf-8")
        time.sleep(hold)
        end = time.time()
    out.write_text(json.dumps({"start": start, "end": end}), encoding="utf-8")
except browser.BrowserError as exc:
    out.write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
'''


def _pilot_process(cwd, account, out, hold):
    script = cwd / "pilot_script.py"
    script.write_text(_PILOT_SCRIPT, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.Popen([sys.executable, str(script), account, str(out), str(hold)], cwd=cwd, env=env)


def _wait_for(path, timeout=30):
    import time

    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} jamais écrit"
        time.sleep(0.05)


def test_two_processes_never_drive_at_the_same_time(cwd):
    first = _pilot_process(cwd, "compte_a", cwd / "a.json", 1.5)
    _wait_for(cwd / "a.json.inside")
    second = _pilot_process(cwd, "compte_b", cwd / "b.json", 0.2)
    assert first.wait(60) == 0 and second.wait(60) == 0

    a = json.loads((cwd / "a.json").read_text(encoding="utf-8"))
    b = json.loads((cwd / "b.json").read_text(encoding="utf-8"))
    assert b["start"] >= a["end"]  # le second processus a attendu la fin du premier pilotage


def test_a_process_gives_up_after_pilot_wait_and_names_the_account_driven_by_another_process(cwd):
    import time

    holder = _pilot_process(cwd, "compte_worker", cwd / "w.json", 3)
    try:
        _wait_for(cwd / "w.json.inside")
        config = Config(mode="review", workspace_dir=cwd / "workspace", output_dir=cwd / "output",
                        _sections={"browser": {"pilot_wait_s": 0.3}})
        fake_playwright_ctx = FakeContext()
        browser.use_playwright(lambda: FakeManager(FakePlaywright(FakeChromium(fake_playwright_ctx))))
        started = time.monotonic()
        with pytest.raises(browser.BrowserError, match="compte_worker") as caught:
            with browser._open_context("compte_web", headless=True, config=config):
                pytest.fail("deux processus pilotent en même temps")
        assert time.monotonic() - started < 2.5  # attente bornee par pilot_wait_s, pas jusqu'a la fin du pilotage
        assert "compte_web" in str(caught.value)
    finally:
        assert holder.wait(60) == 0
    assert (cwd / "state").is_dir() and any(p.name.startswith("pilot") for p in (cwd / "state").iterdir())


def test_the_pilot_lock_is_free_again_once_the_other_process_has_finished(cwd, monkeypatch):
    holder = _pilot_process(cwd, "compte_worker", cwd / "w.json", 0.1)
    assert holder.wait(60) == 0
    fake_playwright(monkeypatch)
    config = Config(mode="review", workspace_dir=cwd / "workspace", output_dir=cwd / "output",
                    _sections={"browser": {"pilot_wait_s": 0.3}})
    with browser._open_context("compte_web", headless=True, config=config):
        pass


# ---------------------------------------------------------------- garde du pays de l'IP (TASK-30cc)


def _net_config(cwd, **network_table):
    return Config(mode="review", workspace_dir=cwd / "w", output_dir=cwd / "o", _sections={"network": network_table})


def test_open_context_refuses_outside_expected_country(cwd, monkeypatch):
    fake_playwright(monkeypatch)
    network.use_fetcher(lambda url: {"ip": "5.6.7.8", "city": "London", "country": "GB", "org": "AS1 BT"})
    with pytest.raises(browser.BrowserError) as caught:
        with browser._open_context("ab12cd", headless=True):
            pytest.fail("le navigateur ne doit pas s'ouvrir hors pays")
    assert str(caught.value) == "IP en Royaume-Uni (attendu France) : passe sur le partage de connexion du téléphone"


def test_open_context_refuses_when_country_unknown(cwd, monkeypatch):
    fake_playwright(monkeypatch)

    def down(url):
        raise OSError("hors ligne")

    network.use_fetcher(down)
    with pytest.raises(browser.BrowserError, match="pays de l'IP inconnu"):
        with browser._open_context("ab12cd", headless=True):
            pytest.fail("le navigateur ne doit pas s'ouvrir")


def test_open_context_opens_in_expected_country(cwd, monkeypatch):
    context, _chromium, _pw = fake_playwright(monkeypatch)
    with browser._open_context("ab12cd", headless=True) as opened:
        assert opened is context


def test_open_context_free_when_block_disabled(cwd, monkeypatch):
    context, _chromium, _pw = fake_playwright(monkeypatch)
    network.use_fetcher(lambda url: pytest.fail("aucun appel quand block_browser = false"))
    with browser._open_context("ab12cd", headless=True, config=_net_config(cwd, block_browser=False)) as opened:
        assert opened is context


def test_open_context_unknown_country_is_unavailable_but_other_country_is_a_plain_refusal(cwd, monkeypatch):
    """I2 : pays inconnu = BrowserUnavailable (condition transitoire) ; pays différent = BrowserError simple."""
    fake_playwright(monkeypatch)

    def down(url):
        raise OSError("hors ligne")

    network.use_fetcher(down)
    with pytest.raises(browser.BrowserUnavailable):
        with browser._open_context("ab12cd", headless=True):
            pytest.fail("le navigateur ne doit pas s'ouvrir")
    network.reset()
    network.use_fetcher(lambda url: {"ip": "5.6.7.8", "city": "London", "country": "GB", "org": "AS1 BT"})
    with pytest.raises(browser.BrowserError) as caught:
        with browser._open_context("ab12cd", headless=True):
            pytest.fail("le navigateur ne doit pas s'ouvrir hors pays")
    assert not isinstance(caught.value, browser.BrowserUnavailable)


def test_profile_is_looked_up_in_the_configured_browser_state_dir(cwd):
    config = Config(mode="review", workspace_dir=cwd / "w", output_dir=cwd / "o",
                    _sections={"browser": {"state_dir": str(cwd / "ailleurs" / "profils")}})
    profile = cwd / "ailleurs" / "profils" / "ab12cd"
    profile.mkdir(parents=True)
    (profile / "Local State").write_text("{}", encoding="utf-8")

    assert browser.profile_dir("ab12cd", config) == profile
    assert browser.profile_status("ab12cd", config)["present"] is True
    assert browser.profile_status("ab12cd")["present"] is False  # défaut state/browser : rien là-bas
    assert browser._pilot_paths(config)[0] == cwd / "ailleurs" / "pilot.lock"
    assert browser.CONFIG_DEFAULTS["state_dir"] == "state/browser"


# ------------------------------------------------ TASK-ef6e6c31363f : pays attendu par compte


def _uk_account_config(cwd):
    _config_file(cwd, '{"accounts": [{"id": "ab12cd", "label": "ClipManiaq3", "expected_country": "GB"}, '
                      '{"id": "ef34ab", "label": "Compte FR"}]}')
    return Config(mode="review", workspace_dir=cwd / "w", output_dir=cwd / "o")


def test_open_context_uk_account_opens_on_uk_ip(cwd, monkeypatch):
    context, _chromium, _pw = fake_playwright(monkeypatch)
    network.use_fetcher(lambda url: {"ip": "5.6.7.8", "city": "London", "country": "GB", "org": "AS1 BT"})
    with browser._open_context("ab12cd", headless=True, config=_uk_account_config(cwd)) as opened:
        assert opened is context


def test_open_context_uk_account_refuses_fr_ip_naming_account_and_countries(cwd, monkeypatch):
    fake_playwright(monkeypatch)
    with pytest.raises(browser.BrowserError) as caught:
        with browser._open_context("ab12cd", headless=True, config=_uk_account_config(cwd)):
            pytest.fail("le navigateur ne doit pas s'ouvrir")
    assert str(caught.value) == ("compte ClipManiaq3 : IP en France (attendu Royaume-Uni) : "
                                 "passe sur le partage de connexion du téléphone")


def test_open_context_account_without_country_refuses_uk_ip_like_before(cwd, monkeypatch):
    fake_playwright(monkeypatch)
    network.use_fetcher(lambda url: {"ip": "5.6.7.8", "city": "London", "country": "GB", "org": "AS1 BT"})
    with pytest.raises(browser.BrowserError) as caught:
        with browser._open_context("ef34ab", headless=True, config=_uk_account_config(cwd)):
            pytest.fail("le navigateur ne doit pas s'ouvrir")
    assert str(caught.value) == "IP en Royaume-Uni (attendu France) : passe sur le partage de connexion du téléphone"
