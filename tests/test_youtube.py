"""Tests de clipper.youtube, des comptes YouTube et du pilotage commun (TASK-883c, SPEC-5e50 R1, R5, R8).

Aucun test ne touche YouTube, le reseau ni un vrai navigateur : fausses pages, faux Playwright.
"""

from __future__ import annotations

import json
import random
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipper import accounts, browser, youtube
from clipper.config import Config
from clipper.web import create_app

ROOT = Path(__file__).resolve().parent.parent
SELECTORS = ROOT / "clipper" / "assets" / "youtube_selectors.toml"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
UC = "UCabcdefghijklmnopqrstuv"
STUDIO = "https://studio.youtube.com"
CHANNEL_URL = f"{STUDIO}/channel/{UC}/videos/short"
LOGIN_URL = "https://accounts.google.com/v3/signin/identifier?service=youtube"


def _sel() -> dict:
    return youtube.load_selectors()


def make_config(tmp_path) -> Config:
    return Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output")


# ---------------------------------------------------------------- fausse page YouTube Studio


class FakeElement:
    def __init__(self, page, text="", on_click=None, visible=True):
        self.page, self.text, self.on_click, self.visible = page, text, on_click, visible
        self.children: dict[str, "FakeElement"] = {}

    def click(self, **kwargs):
        self.page.calls.append(("click", self.text))
        if self.on_click is not None:
            self.on_click()

    def inner_text(self):
        return self.text

    def is_visible(self):
        return self.visible

    def query_selector(self, selector):
        return self.children.get(selector)


class FakeStudio:
    """YouTube Studio simule : ``redirect`` = adresse reelle apres goto ; ``nav_text`` = texte de la navigation ;
    ``dialogs`` = fenetres surgissantes (texte, libelle du bouton)."""

    def __init__(self, *, redirect=CHANNEL_URL, nav_text="Votre chaîne Ma Chaîne\nTableau de bord\nContenu",
                 dialogs=(), screenshot_error=None):
        self.url = "about:blank"
        self.redirect, self.nav_text, self.screenshot_error = redirect, nav_text, screenshot_error
        self.calls: list[tuple] = []
        self.dialogs: list[FakeElement] = []
        self.closed: list[str] = []
        for text, label in dialogs:
            self.add_dialog(text, label)

    def add_dialog(self, text, label):
        dialog = FakeElement(self, text=text)
        button_sel = _sel()["modal"]["button"].format(label=label)
        dialog.children[button_sel] = FakeElement(self, text=label, on_click=lambda: self.close(dialog, label))
        self.dialogs.append(dialog)

    def close(self, dialog, label):
        self.dialogs.remove(dialog)
        self.closed.append(label)

    def goto(self, url, **kwargs):
        self.calls.append(("goto", url))
        self.url = self.redirect

    def query_selector_all(self, selector):
        if selector == _sel()["modal"]["container"]:
            return list(self.dialogs)
        if selector == _sel()["selectors"]["navigation"] and self.nav_text is not None:
            return [FakeElement(self, text=self.nav_text)]
        return []

    def query_selector(self, selector):
        found = self.query_selector_all(selector)
        return found[0] if found else None

    def wait_for_timeout(self, ms):
        self.calls.append(("poll", ms))

    def screenshot(self, path=None, **kwargs):
        if self.screenshot_error:
            raise RuntimeError(self.screenshot_error)
        Path(path).write_bytes(b"\x89PNG fake")


class FakeContext:
    def __init__(self, page):
        self.page, self.pages = page, [page]

    def new_page(self):
        return self.page


def verify(tmp_path, monkeypatch, page, **settings):
    monkeypatch.chdir(tmp_path)
    opened = []
    sleeps = []

    @contextmanager
    def opener(account, *, headless):
        opened.append((account, headless))
        yield FakeContext(page)

    config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "o",
                    _sections={"youtube": settings})
    result = youtube.verify_login("ma_chaine", config=config, now=NOW, opener=opener, sleep=sleeps.append,
                                  rng=random.Random(1))
    return result, opened, sleeps


# ---------------------------------------------------------------- reglages et reperes


def test_youtube_defaults_follow_r5():
    d = youtube.CONFIG_DEFAULTS
    assert (d["max_posts_per_day"], d["min_gap_minutes"]) == (3, 120)
    assert d["min_action_delay_s"] <= d["max_action_delay_s"]


def test_youtube_section_resolves_through_clipper_config():
    config = Config(mode="review", workspace_dir=Path("w"), output_dir=Path("o"), _sections={"youtube": {"max_posts_per_day": 2}})
    assert config.section("youtube")["max_posts_per_day"] == 2
    assert youtube.get_settings(config)["max_posts_per_day"] == 2


@pytest.mark.parametrize("key,value", [("max_posts_per_day", 0), ("min_gap_minutes", -1), ("action_timeout_s", 0),
                                       ("max_action_delay_s", True)])
def test_an_invalid_youtube_setting_is_an_explicit_error(key, value):
    config = Config(mode="review", workspace_dir=Path("w"), output_dir=Path("o"), _sections={"youtube": {key: value}})
    with pytest.raises(youtube.YouTubeError, match=key):
        youtube.get_settings(config)


def test_min_delay_above_max_delay_is_an_explicit_error():
    config = Config(mode="review", workspace_dir=Path("w"), output_dir=Path("o"),
                    _sections={"youtube": {"min_action_delay_s": 5, "max_action_delay_s": 1}})
    with pytest.raises(youtube.YouTubeError, match="min_action_delay_s"):
        youtube.get_settings(config)


def test_the_selectors_file_is_loaded_and_holds_the_surveyed_landmarks():
    data = _sel()
    assert data["urls"]["studio"] == STUDIO
    assert re.search(data["expect"]["channel_url_pattern"], CHANNEL_URL).group(1) == UC
    assert "accounts.google.com" in data["expect"]["login_url_markers"]
    assert data["labels"]["channel_prefix"] == "Votre chaîne"
    assert data["popups"] == {"Bienvenue dans YouTube Studio": "Continuer",
                              "tout le monde peut regarder les vidéos publiques": "OK"}


def test_the_surveyed_landmarks_are_marked_verified_on_2026_10_03():
    text = SELECTORS.read_text(encoding="utf-8")
    assert text.count("vérifié 2026-10-03") >= 4
    assert "NON VÉRIFIÉ" in text  # un repere ecrit sans releve reel ne se fait pas passer pour verifie (R4)


@pytest.mark.parametrize("table,key", [("urls", "studio"), ("expect", "channel_url_pattern"),
                                       ("expect", "login_url_markers"), ("labels", "channel_prefix"),
                                       ("selectors", "navigation"), ("modal", "container"), ("modal", "button")])
def test_a_missing_selector_is_an_explicit_error_naming_the_key(tmp_path, table, key):
    text = SELECTORS.read_text(encoding="utf-8")
    broken = re.sub(rf"(?m)^{key} = ", f"{key}_absent = ", text, count=1)
    assert broken != text
    bad = tmp_path / "s.toml"
    bad.write_text(broken, encoding="utf-8")
    with pytest.raises(youtube.YouTubeError, match=rf"\[{table}\] {key}"):
        youtube.load_selectors(bad)


def test_a_missing_popups_table_is_an_explicit_error(tmp_path):
    text = SELECTORS.read_text(encoding="utf-8").replace("[popups]", "[popups_absent]")
    bad = tmp_path / "s.toml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(youtube.YouTubeError, match=r"\[popups\]"):
        youtube.load_selectors(bad)


def test_a_missing_selectors_file_is_an_explicit_error(tmp_path):
    with pytest.raises(youtube.YouTubeError, match="introuvable"):
        youtube.load_selectors(tmp_path / "absent.toml")


def test_the_channel_url_pattern_must_capture_the_channel_id(tmp_path):
    text = SELECTORS.read_text(encoding="utf-8")
    broken = re.sub(r"(?m)^channel_url_pattern = .*$", "channel_url_pattern = 'studio\\.youtube\\.com/channel/'", text)
    bad = tmp_path / "s.toml"
    bad.write_text(broken, encoding="utf-8")
    with pytest.raises(youtube.YouTubeError, match="channel_url_pattern"):
        youtube.load_selectors(bad)


# ---------------------------------------------------------------- verification « pret a publier » (R1)


def test_a_connected_studio_gives_ready_with_the_channel_name_and_id(tmp_path, monkeypatch):
    page = FakeStudio()
    result, opened, sleeps = verify(tmp_path, monkeypatch, page)

    assert result["ready"] is True
    assert result["channel"] == {"name": "Ma Chaîne", "id": UC}
    assert result["reason"] is None
    assert ("goto", STUDIO) in page.calls
    assert opened == [("ma_chaine", False)]  # visible : jamais de navigateur cache (ADR-58c0)


def test_the_channel_name_is_read_after_the_label_in_the_navigation(tmp_path, monkeypatch):
    page = FakeStudio(nav_text="Menu\nVotre chaîne   Les Aventures de Léa \nContenu")
    result, _, _ = verify(tmp_path, monkeypatch, page)
    assert result["channel"]["name"] == "Les Aventures de Léa"


def test_the_google_login_page_is_not_ready_with_a_reason(tmp_path, monkeypatch):
    page = FakeStudio(redirect=LOGIN_URL, nav_text=None)
    result, _, _ = verify(tmp_path, monkeypatch, page)

    assert result["ready"] is False
    assert result["channel"] is None
    assert "connexion Google" in result["reason"] and "Se connecter" in result["reason"]


def test_a_google_verification_page_is_not_ready_too(tmp_path, monkeypatch):
    page = FakeStudio(redirect="https://accounts.google.com/v3/signin/challenge/pwd", nav_text=None)
    result, _, _ = verify(tmp_path, monkeypatch, page)
    assert result["ready"] is False and "connexion Google" in result["reason"]


def test_an_unexpected_page_is_not_ready_with_the_address_and_a_capture(tmp_path, monkeypatch):
    page = FakeStudio(redirect="https://www.youtube.com/", nav_text=None)
    result, _, _ = verify(tmp_path, monkeypatch, page, action_timeout_s=2, poll_interval_s=1)

    assert result["ready"] is False
    assert "https://www.youtube.com/" in result["reason"]
    assert result["capture"] and Path(result["capture"]).is_file()
    assert Path(result["capture"]).parent == browser.profile_dir("ma_chaine") / "captures"
    assert [c for c in page.calls if c[0] == "poll"]  # attente bornee de l'adresse de chaine


def test_a_channel_page_without_the_channel_label_is_not_ready(tmp_path, monkeypatch):
    page = FakeStudio(nav_text="Tableau de bord\nContenu")
    result, _, _ = verify(tmp_path, monkeypatch, page)
    assert result["ready"] is False and "Votre chaîne" in result["reason"]


def test_the_channel_name_on_the_next_line_as_on_the_real_page_is_read(tmp_path, monkeypatch):
    # Relevé réel 2026-10-03 : la navigation affiche « Votre chaîne », puis le nom sur la ligne suivante.
    page = FakeStudio(nav_text="Votre chaîne\nMa Chaîne\nTableau de bord\nContenus")
    result, _, _ = verify(tmp_path, monkeypatch, page)
    assert result["ready"] is True and result["channel"]["name"] == "Ma Chaîne"


def test_an_empty_channel_name_is_not_ready(tmp_path, monkeypatch):
    page = FakeStudio(nav_text="Votre chaîne   \n\nContenu")
    result, _, _ = verify(tmp_path, monkeypatch, page)
    assert result["ready"] is False and "nom" in result["reason"]


def test_the_welcome_dialog_is_closed_with_continue_when_present(tmp_path, monkeypatch):
    page = FakeStudio(dialogs=[("Bienvenue dans YouTube Studio Gérez votre chaîne", "Continuer")])
    result, _, _ = verify(tmp_path, monkeypatch, page)

    assert page.closed == ["Continuer"]
    assert result["ready"] is True


def test_no_welcome_dialog_means_no_click(tmp_path, monkeypatch):
    page = FakeStudio()
    verify(tmp_path, monkeypatch, page)
    assert not [c for c in page.calls if c[0] == "click"]


def test_an_unknown_dialog_is_not_ready_and_never_clicked(tmp_path, monkeypatch):
    page = FakeStudio(dialogs=[("Une fenêtre jamais vue", "OK")])
    result, _, _ = verify(tmp_path, monkeypatch, page)

    assert result["ready"] is False and "Une fenêtre jamais vue" in result["reason"]
    assert page.closed == []


def test_a_known_dialog_without_its_button_is_not_ready(tmp_path, monkeypatch):
    page = FakeStudio()
    dialog = FakeElement(page, text="Bienvenue dans YouTube Studio")
    page.dialogs.append(dialog)
    result, _, _ = verify(tmp_path, monkeypatch, page)
    assert result["ready"] is False and "Continuer" in result["reason"]


def test_the_verification_pauses_like_a_human(tmp_path, monkeypatch):
    page = FakeStudio(dialogs=[("Bienvenue dans YouTube Studio", "Continuer")])
    _, _, sleeps = verify(tmp_path, monkeypatch, page, min_action_delay_s=0.2, max_action_delay_s=0.4)
    assert sleeps and all(0.2 <= s <= 0.4 for s in sleeps)


def test_a_failed_capture_is_said_in_the_reason_never_swallowed(tmp_path, monkeypatch):
    page = FakeStudio(redirect="https://www.youtube.com/", nav_text=None, screenshot_error="boom")
    result, _, _ = verify(tmp_path, monkeypatch, page, action_timeout_s=1, poll_interval_s=1)
    assert result["capture"] is None and "capture d'écran impossible" in result["reason"]


def test_an_invalid_account_id_is_refused_before_any_browser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(browser.BrowserError, match="identifiant de compte invalide"):
        youtube.verify_login("../x", opener=lambda *a, **k: pytest.fail("navigateur ouvert"))


def test_the_default_opener_is_the_shared_browser_context_with_paris_time(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    page = FakeStudio()
    context = FakeBrowserContext(page)
    chromium = install_fake_playwright(monkeypatch, context)

    result = youtube.verify_login("ma_chaine", now=NOW, sleep=lambda s: None, rng=random.Random(1))

    assert result["ready"] is True
    (_, kwargs), = chromium.launches
    assert kwargs["timezone_id"] == "Europe/Paris" and kwargs["headless"] is False and context.closed


# ---------------------------------------------------------------- navigateur : heure de Paris et verrou (R8, R5)


class FakeBrowserContext(FakeContext):
    def __init__(self, page=None, on_close=None):
        super().__init__(page or FakeStudio())
        self.closed, self._on_close = False, on_close

    def add_init_script(self, script):
        pass

    def close(self):
        self.closed = True
        if self._on_close:
            self._on_close()


class FakeChromium:
    def __init__(self, contexts):
        self.contexts, self.launches = list(contexts), []

    def launch_persistent_context(self, user_data_dir, **kwargs):
        self.launches.append((str(user_data_dir), kwargs))
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        return self.contexts.pop(0) if len(self.contexts) > 1 else self.contexts[0]


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium

    def stop(self):
        pass


class FakeManager:
    def __init__(self, chromium):
        self.chromium = chromium

    def start(self):
        return FakePlaywright(self.chromium)


def install_fake_playwright(monkeypatch, *contexts):
    chromium = FakeChromium(contexts)
    browser.use_playwright(lambda: FakeManager(chromium))
    monkeypatch.setattr(browser, "_active", {})
    return chromium


@pytest.fixture(autouse=True)
def _reset_browser():
    yield
    browser.use_playwright(None)


def test_every_driven_context_is_launched_with_the_paris_timezone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    chromium = install_fake_playwright(monkeypatch, FakeBrowserContext())

    with browser._open_context("ma_chaine", headless=False):
        pass

    (_, kwargs), = chromium.launches
    assert kwargs["timezone_id"] == "Europe/Paris"
    assert browser.TIMEZONE == "Europe/Paris"


def test_two_accounts_are_never_driven_at_the_same_time_whatever_their_service(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    install_fake_playwright(monkeypatch, FakeBrowserContext(), FakeBrowserContext())
    monkeypatch.setitem(browser.CONFIG_DEFAULTS, "pilot_wait_s", 0.1)

    with browser._open_context("compte_tiktok", headless=False):
        with pytest.raises(browser.BrowserError, match="compte_tiktok"):
            with browser._open_context("compte_youtube", headless=False):
                pytest.fail("deux comptes pilotes en meme temps")


def test_the_second_account_waits_for_the_first_then_goes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    install_fake_playwright(monkeypatch, FakeBrowserContext(), FakeBrowserContext())
    monkeypatch.setitem(browser.CONFIG_DEFAULTS, "pilot_wait_s", 10)
    order: list[str] = []
    inside = threading.Event()
    release = threading.Event()

    def first():
        with browser._open_context("compte_tiktok", headless=False):
            order.append("tiktok-in")
            inside.set()
            release.wait(5)
            order.append("tiktok-out")

    def second():
        with browser._open_context("compte_youtube", headless=False):
            order.append("youtube-in")

    t1 = threading.Thread(target=first)
    t1.start()
    assert inside.wait(5)
    t2 = threading.Thread(target=second)
    t2.start()
    t2.join(0.3)
    assert t2.is_alive() and order == ["tiktok-in"]  # le second attend : jamais en meme temps
    release.set()
    t1.join(5)
    t2.join(5)
    assert order == ["tiktok-in", "tiktok-out", "youtube-in"]


def test_the_lock_is_released_when_the_launch_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    chromium = install_fake_playwright(monkeypatch, FakeBrowserContext())
    monkeypatch.setitem(browser.CONFIG_DEFAULTS, "pilot_wait_s", 0.1)

    def boom(*a, **k):
        raise RuntimeError("Chromium distribution 'chrome' is not found")

    real = chromium.launch_persistent_context
    chromium.launch_persistent_context = boom
    with pytest.raises(browser.BrowserError, match="Chrome est introuvable"):
        with browser._open_context("compte_tiktok", headless=False):
            pass
    chromium.launch_persistent_context = real
    with browser._open_context("compte_youtube", headless=False):  # ne bloque pas
        pass


def test_the_pilot_wait_is_a_browser_setting():
    assert browser.CONFIG_DEFAULTS["pilot_wait_s"] > 0


def test_youtube_login_opens_a_normal_chrome_on_studio_never_playwright(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    launched = []

    class Proc:
        def wait(self):
            return 0

    monkeypatch.setattr(browser, "_popen", lambda cmd, **kw: launched.append(cmd) or Proc())
    monkeypatch.setattr(browser, "find_chrome", lambda config=None: Path("chrome"))
    browser.use_playwright(lambda: pytest.fail("Playwright lance pour la connexion"))

    browser.login("ma_chaine", youtube.studio_url())

    (cmd,) = launched
    assert cmd[-1] == STUDIO
    assert any(a.startswith("--user-data-dir=") and a.endswith("ma_chaine") for a in cmd)


# ---------------------------------------------------------------- comptes : service et migration (R1)


def _write_accounts(tmp_path, accounts_list):
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "accounts.json").write_text(json.dumps({"accounts": accounts_list}), encoding="utf-8")


def test_existing_accounts_without_a_service_become_tiktok_on_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_accounts(tmp_path, [{"id": "ab12cd", "label": "Ancien compte", "platform": "TikTok", "username": "",
                                "notes": "", "has_password": False}])

    listed = accounts.list_accounts(make_config(tmp_path))

    assert [a["service"] for a in listed] == ["tiktok"]


def test_the_migration_is_written_on_the_next_save_without_user_action(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_accounts(tmp_path, [{"id": "ab12cd", "label": "Ancien compte", "platform": "", "username": "",
                                "notes": "", "has_password": False}])
    config = make_config(tmp_path)

    accounts.update_account(config, "ab12cd", {"notes": "x"})

    saved = json.loads((tmp_path / "state" / "accounts.json").read_text(encoding="utf-8"))
    assert saved["accounts"][0]["service"] == "tiktok"


def test_an_account_is_added_with_a_service_defaulting_to_tiktok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = make_config(tmp_path)

    tiktok = accounts.add_account(config, {"label": "Compte A"})
    yt = accounts.add_account(config, {"label": "ma_chaine", "service": "youtube"})

    assert tiktok["service"] == "tiktok" and yt["service"] == "youtube"
    assert [a["service"] for a in accounts.list_accounts(config)] == ["tiktok", "youtube"]


@pytest.mark.parametrize("bad", ["twitch", "", 3, None])
def test_an_unknown_service_is_an_explicit_error(tmp_path, monkeypatch, bad):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(accounts.AccountsError, match="service"):
        accounts.add_account(make_config(tmp_path), {"label": "x", "service": bad})


def test_the_service_of_an_account_cannot_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = make_config(tmp_path)
    created = accounts.add_account(config, {"label": "ma_chaine", "service": "youtube"})

    with pytest.raises(accounts.AccountsError, match="service"):
        accounts.update_account(config, created["id"], {"service": "tiktok"})
    assert accounts.update_account(config, created["id"], {"service": "youtube", "notes": "ok"})["notes"] == "ok"


def test_a_youtube_account_is_not_ready_until_its_channel_is_verified(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = make_config(tmp_path)
    created = accounts.add_account(config, {"label": "ma_chaine", "service": "youtube"})

    assert accounts.ready_blocked_reason(created) and "YouTube" in accounts.ready_blocked_reason(created)
    done = accounts.record_login(config, created["id"], {
        "state": "connected", "channel": {"name": "Ma Chaîne", "id": UC}})

    assert done["ready_to_publish"] is True and done["auto_checked"] is True
    assert done["channel"] == {"name": "Ma Chaîne", "id": UC}
    assert accounts.ready_blocked_reason(done) is None


def test_a_google_login_page_unchecks_the_account_with_the_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = make_config(tmp_path)
    created = accounts.add_account(config, {"label": "ma_chaine", "service": "youtube"})
    accounts.record_login(config, created["id"], {"state": "connected", "channel": {"name": "Ma Chaîne", "id": UC}})

    out = accounts.record_login(config, created["id"], {"state": "never", "reason": "page de connexion Google affichée"})

    assert out["ready_to_publish"] is False and out["auto_unchecked"] is True
    assert "YouTube" in out["ready_note"]
    assert accounts.ready_blocked_reason(out)


def test_a_connected_observation_without_a_channel_is_refused_for_youtube(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = make_config(tmp_path)
    created = accounts.add_account(config, {"label": "ma_chaine", "service": "youtube"})
    with pytest.raises(accounts.AccountsError, match="chaîne"):
        accounts.record_login(config, created["id"], {"state": "connected"})


# ---------------------------------------------------------------- API web (R1)


def local_client(tmp_path) -> TestClient:
    return TestClient(create_app(config=make_config(tmp_path)), base_url="http://127.0.0.1:8000",
                      client=("127.0.0.1", 50000))


def test_a_youtube_account_is_added_through_the_web_api_and_listed_with_its_service(tmp_path, isolated_cwd):
    c = local_client(tmp_path)

    created = c.post("/api/accounts", json={"label": "ma_chaine", "service": "youtube"})
    assert created.status_code == 201 and created.json()["service"] == "youtube"
    c.post("/api/accounts", json={"label": "Compte TikTok"})

    listed = c.get("/api/accounts").json()
    assert {a["label"]: a["service"] for a in listed} == {"ma_chaine": "youtube", "Compte TikTok": "tiktok"}


def test_the_web_api_refuses_an_unknown_service(tmp_path, isolated_cwd):
    resp = local_client(tmp_path).post("/api/accounts", json={"label": "x", "service": "twitch"})
    assert resp.status_code == 422 and "service" in resp.json()["detail"]


def test_listing_a_youtube_account_never_reads_tiktok_cookies(tmp_path, isolated_cwd, monkeypatch):
    c = local_client(tmp_path)
    c.post("/api/accounts", json={"label": "ma_chaine", "service": "youtube"})
    monkeypatch.setattr(browser, "login_state", lambda *a, **k: pytest.fail("cookies TikTok lus pour un compte YouTube"))

    listed = c.get("/api/accounts").json()

    assert listed[0]["service"] == "youtube" and listed[0]["ready_to_publish"] is False
    assert "YouTube" in listed[0]["ready_blocked_reason"]


def test_youtube_login_opens_studio_by_default_and_verifies_the_channel_on_close(tmp_path, isolated_cwd, monkeypatch):
    c = local_client(tmp_path)
    account = c.post("/api/accounts", json={"label": "ma_chaine", "service": "youtube"}).json()
    seen = {}

    def fake_start_login(account_id, url=None, *, config=None, on_close=None):
        seen.update(account=account_id, url=url)
        on_close()  # l'utilisateur ferme la fenetre

    monkeypatch.setattr(browser, "start_login", fake_start_login)
    monkeypatch.setattr(youtube, "verify_login", lambda account_id, **kw: {
        "ready": True, "reason": None, "channel": {"name": "Ma Chaîne", "id": UC}, "capture": None})

    resp = c.post(f"/api/accounts/{account['id']}/browser/login", json={})

    assert resp.status_code == 202
    assert seen == {"account": account["id"], "url": STUDIO}
    listed = c.get("/api/accounts").json()[0]
    assert listed["ready_to_publish"] is True and listed["channel"] == {"name": "Ma Chaîne", "id": UC}


def test_the_verify_route_records_a_not_ready_youtube_account_with_the_reason(tmp_path, isolated_cwd, monkeypatch):
    c = local_client(tmp_path)
    account = c.post("/api/accounts", json={"label": "ma_chaine", "service": "youtube"}).json()
    monkeypatch.setattr(youtube, "verify_login", lambda account_id, **kw: {
        "ready": False, "reason": "page de connexion Google affichée : connecte-toi", "channel": None, "capture": None})

    resp = c.post(f"/api/accounts/{account['id']}/verify", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ready_to_publish"] is False and "connexion" in body["ready_blocked_reason"]


def test_the_verify_route_reports_a_browser_error_without_fallback(tmp_path, isolated_cwd, monkeypatch):
    c = local_client(tmp_path)
    account = c.post("/api/accounts", json={"label": "ma_chaine", "service": "youtube"}).json()

    def boom(account_id, **kw):
        raise browser.BrowserError("Chrome est introuvable : installe Google Chrome")

    monkeypatch.setattr(youtube, "verify_login", boom)

    resp = c.post(f"/api/accounts/{account['id']}/verify", json={})

    assert resp.status_code == 409 and "Chrome est introuvable" in resp.json()["detail"]
    assert c.get("/api/accounts").json()[0]["ready_to_publish"] is False


def test_the_verify_route_refuses_a_tiktok_account_and_an_unknown_account(tmp_path, isolated_cwd):
    c = local_client(tmp_path)
    account = c.post("/api/accounts", json={"label": "Compte TikTok"}).json()
    assert c.post(f"/api/accounts/{account['id']}/verify", json={}).status_code == 409
    assert c.post("/api/accounts/inconnu/verify", json={}).status_code == 404


# ---------------------------------------------------------------- ecran Comptes


STATIC = ROOT / "clipper" / "web" / "static"


def test_the_accounts_screen_offers_the_service_choice_and_shows_it_on_each_account():
    js = (STATIC / "screens" / "accounts.js").read_text(encoding="utf-8")
    assert 'name="service"' in js and "YouTube" in js
    assert "a.service" in js
    assert "/verify" in js


# ================================================================ publication (TASK-9776, SPEC-5e50 R2-R5, R8)
#
# Fausse dialog d'envoi de YouTube Studio : aucune vraie page, aucun navigateur. Les selecteurs viennent du toml
# (le test les lit comme le code), les actions de la page modifient l'etat visible.

from datetime import timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

SHORT_ID = "OOOeOwbvu34"
# Entrees reelles du menu « Créer » (relevé 2026-10-03, Chrome piloté : research/youtube-inspect/yt_probe2.py).
DEFAULT_MENU_ITEMS = ("Importer des vidéos", "Passer au direct", "Nouvelle playlist", "Nouveau podcast")
LONDON = ZoneInfo("Europe/London")
PARIS = ZoneInfo("Europe/Paris")


class FakeTimeoutError(Exception):
    """Nom contenant « Timeout » comme celui de Playwright."""


class El:
    def __init__(self, page, text="", on_click=None, attrs=None, value=None, enabled=True):
        self.page, self.text, self.on_click = page, text, on_click
        self.attrs, self.value, self.enabled, self.visible = dict(attrs or {}), value, enabled, True
        self.children: dict[str, "El"] = {}

    def click(self, **kwargs):
        self.page.calls.append(("click", self.text or self.value))
        if self.on_click is not None:
            self.on_click()

    def fill(self, text, **kwargs):
        self.page.calls.append(("fill", text))
        self.value = text

    def press(self, key, **kwargs):
        self.page.calls.append(("press", key))
        if key == "Enter":
            self.page.committed(self)

    def inner_text(self):
        return self.text

    def input_value(self):
        return self.value

    def get_attribute(self, name):
        return self.attrs.get(name)

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def query_selector(self, selector):
        return self.children.get(selector)


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        self.page.calls.append(("key", key))
        if key == "Backspace" and self.page.focus is not None:
            self.page.typed[self.page.focus] = ""

    def insert_text(self, text):
        self.page.calls.append(("insert", text))
        self.page.typed[self.page.focus] = self.page.typed.get(self.page.focus, "") + text


class FakeUpload:
    """Dialog d'envoi de YouTube Studio : etapes Details -> Elements video -> Verifications -> Visibilite.

    ``tz_default`` : fuseau propose par defaut (PC simule a Londres : « Heure locale »). ``welcome`` : la dialog
    « Bienvenue » est presente. ``upload_polls`` : lectures avant que le bouton final soit actif. ``success_polls`` :
    lectures avant la fenetre de succes."""

    def __init__(self, *, welcome=True, studio_redirect=CHANNEL_URL, menu_items=DEFAULT_MENU_ITEMS, link=True,
                 upload_polls=0, success_polls=0, success_text=None, drop_tz_paris=False, ignore_date=False,
                 ignore_tz=False, text_boxes=2, captcha=False, final_label=None, screenshot_error=None,
                 public_notice=False):
        self.sel = _sel()
        self.S, self.L = self.sel["selectors"], self.sel["labels"]
        self.url = "about:blank"
        self.calls: list[tuple] = []
        self.studio_redirect = studio_redirect
        self.menu_open = False
        self.menu_items = list(menu_items)
        self.menu_clicked = None
        self.keyboard = FakeKeyboard(self)
        self.focus = None
        self.typed: dict = {}
        self.step = -1                      # -1 : pas de fichier ; 0..3 : etapes
        self.files: list[str] = []
        self.kids = None
        self.visibility = None
        self.expanded = False
        self.date_open = False
        self.tz_open = False
        self.tz_choice = "(GMT+0100) Heure locale"
        self.date_text, self.time_text = "4 oct. 2026", "00:00"
        self.link, self.drop_tz_paris, self.ignore_date, self.ignore_tz = link, drop_tz_paris, ignore_date, ignore_tz
        self.text_boxes_count, self.captcha, self.screenshot_error = text_boxes, captcha, screenshot_error
        self.upload_polls, self.success_polls, self.final_label = upload_polls, success_polls, final_label
        self.final_clicked = False
        self.success_text = success_text
        self.polls_final = 0
        self.polls_success = 0
        self.welcome = welcome
        self.public_notice = public_notice  # rappel de la 1re publication publique, apres « Publique »
        self.clicks_log: list[str] = []
        self.upload_dialog = El(self, text="Importer des vidéos")
        self.upload_dialog.children[self.S["file_input"]] = El(self)

    # -- helpers pour les tests
    def clicked(self, label):
        return any(c == ("click", label) for c in self.calls)

    def title_typed(self):
        return self.typed.get("title")

    def description_typed(self):
        return self.typed.get("description")

    # -- page
    def goto(self, url, **kwargs):
        self.calls.append(("goto", url))
        self.url = self.studio_redirect

    def wait_for_timeout(self, ms):
        self.calls.append(("poll", ms))

    def screenshot(self, path=None, **kwargs):
        if self.screenshot_error:
            raise RuntimeError(self.screenshot_error)
        Path(path).write_bytes(b"\x89PNG fake")

    def set_input_files(self, selector, path, **kwargs):
        assert selector == self.S["file_input"]
        self.calls.append(("upload", path))
        self.files.append(path)
        self.step = 0

    def wait_for_selector(self, selector, timeout=None, state=None):
        found = self.query_selector(selector)
        if found is None:
            raise FakeTimeoutError(f"Timeout {timeout} ms : {selector}")
        return found

    def query_selector(self, selector):
        found = self.query_selector_all(selector)
        return found[0] if found else None

    def _button(self, label, on_click=None):
        return El(self, text=label, on_click=on_click)

    def _pick_menu(self, item):
        self.calls.append(("menu", item))
        self.menu_clicked = item
        self.menu_open = False

    def query_selector_all(self, selector):
        S, L = self.S, self.L
        if self.captcha and selector in self.sel["detect"]["captcha"]:
            return [El(self, text="captcha")]
        if selector == self.sel["modal"]["container"]:
            shown = []
            if self.welcome:
                dialog = El(self, text="Bienvenue dans YouTube Studio\nContinuer")
                dialog.children[self.sel["modal"]["button"].format(label="Continuer")] = El(
                    self, text="Continuer", on_click=lambda: setattr(self, "welcome", False))
                shown.append(dialog)
            if self.step == -1:
                shown.append(self.upload_dialog)
            if self.public_notice and self.visibility == "public":
                notice = El(self, text="N'oubliez pas que tout le monde peut regarder les vidéos publiques\nOK")
                notice.children[self.sel["modal"]["button"].format(label="OK")] = El(
                    self, text="OK", on_click=lambda: setattr(self, "public_notice", False))
                shown.append(notice)
            if self.final_clicked:
                self.polls_success += 1
                if self.success_text is not None and self.polls_success > self.success_polls:
                    shown.append(El(self, text=self.success_text))
            return shown
        if selector == S["labeled_button"].format(label=L["create"]):
            return [self._button(L["create"], on_click=lambda: setattr(self, "menu_open", True))]
        if selector == S["menu_item"]:
            if not self.menu_open:
                return []
            return [El(self, text=item, on_click=(lambda i=item: self._pick_menu(i))) for item in self.menu_items]
        if self.step == -1:
            return [El(self)] if selector == S["file_input"] else []
        if selector == S["file_input"]:
            return [El(self)]
        if selector == S["text_box"] and self.step == 0:
            boxes = []
            for index, name in enumerate(("title", "description")[: self.text_boxes_count]):
                box = El(self, text="04.mp4" if name == "title" else "")
                box.on_click = (lambda n=name: setattr(self, "focus", n))
                boxes.append(box)
            return boxes
        if selector == S["video_link"] and self.link:
            return [El(self, text=f"youtube.com/shorts/{SHORT_ID}",
                       attrs={"href": f"https://youtube.com/shorts/{SHORT_ID}"})]
        for label, value in ((L["kids_yes"], True), (L["kids_no"], False)):
            if selector == S["radio"].format(label=label) and self.step == 0:
                return [El(self, text=label, on_click=lambda v=value: setattr(self, "kids", v))]
        if selector == S["labeled_button"].format(label=L["next"]) and self.step in (0, 1, 2):
            return [self._button(L["next"], on_click=lambda: setattr(self, "step", self.step + 1))]
        if self.step == 3:
            return self._visibility_step(selector)
        return []

    def _visibility_step(self, selector):
        S, L = self.S, self.L
        for key in ("public", "unlisted", "private"):
            if selector == S["radio"].format(label=L["visibility_" + key]):
                return [El(self, text=L["visibility_" + key], on_click=lambda k=key: self._choose(k))]
        if selector == S["expand"].format(label=L["expand_schedule"]):
            return [El(self, text=L["expand_schedule"], on_click=lambda: setattr(self, "expanded", True))]
        if selector == S["date_trigger"] and self.expanded:  # 26/10 : la date est une liste déroulante
            return [El(self, text=self.date_text, on_click=lambda: setattr(self, "date_open", True))]
        if selector == S["text_input"] and self.expanded:
            fields = [El(self, value=self.time_text)]
            if self.date_open and not getattr(self, "date_input_missing", False):  # la liste ouverte porte un champ où taper la date
                fields.insert(0, El(self, value=self.date_text))
            return fields
        if selector == S["labeled_button"].format(label=L["timezone_button"]) and self.expanded:
            return [El(self, text=f"{L['timezone_button']}\n{self.tz_choice}",
                       on_click=lambda: setattr(self, "tz_open", True))]
        if selector == S["timezone_option"] and self.tz_open:
            options = [("(GMT+0100) Heure locale"), "(UTC+01:00) Londres", "(UTC+02:00) Paris", "(UTC+02:00) Berlin"]
            if self.drop_tz_paris:
                options = [o for o in options if "Paris" not in o]
            return [El(self, text=o, on_click=lambda o=o: self._pick_tz(o)) for o in options]
        if selector == S["final_button"]:
            self.polls_final += 1
            label = self.final_label or self._final_label()
            return [El(self, text=label, enabled=self.polls_final > self.upload_polls,
                       on_click=self._click_final)]
        return []

    def _click_final(self):
        if self.public_notice and self.visibility == "public":
            raise FakeTimeoutError("Timeout 30000 ms : bouton final sous la fenêtre de rappel")
        self.final_clicked = True

    def _final_label(self):
        if self.expanded:
            return self.L["final_schedule"]
        return self.L["final_save"] if self.visibility in (None, "private") else self.L["final_publish"]

    def _choose(self, key):
        self.visibility = key
        self.expanded = False

    def _pick_tz(self, option):
        if not self.ignore_tz:
            self.tz_choice = option
        self.tz_open = False

    def committed(self, element):
        """Entree dans un champ de la section Programmer."""
        if element.value is None or self.ignore_date:
            return
        if re.match(r"^\d{1,2}:\d{2}$", element.value):
            self.time_text = element.value
        else:
            self.date_text = element.value
            self.date_open = False


def _clip(tmp_path, **extra):
    mp4 = tmp_path / "04.mp4"
    mp4.write_bytes(b"mp4")
    return {"video_path": mp4, "caption": "Une légende", "hashtags": ["#jeu", "#fun"],
            "screen_title": "Le titre d'écran", **extra}


def publish(tmp_path, monkeypatch, page, *, mode="immediate", schedule_at=None, options=None, clip=None, **settings):
    monkeypatch.chdir(tmp_path)
    opened, sleeps = [], []

    @contextmanager
    def opener(account, *, headless):
        opened.append((account, headless))
        yield FakeContext(page)

    config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "o",
                    _sections={"youtube": {"upload_timeout_s": 3, "publish_confirm_timeout_s": 3, "poll_interval_s": 1,
                                           "action_timeout_s": 2, **settings}})
    result = youtube.publish(clip or _clip(tmp_path), "ma_chaine", mode=mode, schedule_at=schedule_at,
                             options=options, config=config, now=NOW, opener=opener, sleep=sleeps.append,
                             rng=random.Random(1))
    publish.opened, publish.sleeps = opened, sleeps
    return result


def stop_of(tmp_path, monkeypatch, page, **kwargs) -> youtube.YouTubeStop:
    with pytest.raises(youtube.YouTubeStop) as caught:
        publish(tmp_path, monkeypatch, page, **kwargs)
    return caught.value


# -- publication immediate


def test_an_immediate_public_publication_records_the_shorts_url(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo mise en ligne")

    result = publish(tmp_path, monkeypatch, page)

    assert result["post_url"] == f"https://youtube.com/shorts/{SHORT_ID}"
    assert (result["post_id"], result["state"]) == (SHORT_ID, "published")
    assert datetime.fromisoformat(result["publish_at"]) == NOW
    assert page.files == [str(tmp_path / "04.mp4")]
    assert page.clicked("Publique") and page.clicked("Publier")
    assert page.kids is False  # « Non, elle n'est pas conçue pour les enfants » par defaut
    assert publish.opened == [("ma_chaine", False)]  # Chrome visible, jamais cache (ADR-58c0)
    urls = [c[1] for c in page.calls if c[0] == "goto"]
    assert urls == [STUDIO]  # un seul goto : jamais /videos/upload, qui redirige vers le tableau de bord
    assert page.clicked("Créer") and page.menu_clicked == "Importer des vidéos"
    order = [c for c in page.calls if c in (("click", "Créer"), ("menu", "Importer des vidéos"), ("upload", str(tmp_path / "04.mp4")))]
    assert order == [("click", "Créer"), ("menu", "Importer des vidéos"), ("upload", str(tmp_path / "04.mp4"))]


def test_the_first_public_notice_over_the_final_button_is_closed_with_ok(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo mise en ligne", public_notice=True)

    result = publish(tmp_path, monkeypatch, page)

    assert result["state"] == "published"
    assert page.public_notice is False and page.clicked("OK") and page.clicked("Publier")


def test_the_processing_window_also_proves_an_immediate_publication(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Traitement de la vidéo\nLe traitement de la version en définition standard (SD)")

    result = publish(tmp_path, monkeypatch, page)

    assert result["state"] == "published"


def test_the_title_defaults_to_the_screen_title_and_the_description_to_caption_hashtags_and_shorts(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo mise en ligne")

    publish(tmp_path, monkeypatch, page)

    assert page.title_typed() == "Le titre d'écran"
    assert page.description_typed() == "Une légende\n\n#jeu #fun #Shorts"


def test_the_title_option_overrides_the_screen_title(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo mise en ligne")

    publish(tmp_path, monkeypatch, page, options={"title": "Mon titre"})

    assert page.title_typed() == "Mon titre"


def test_the_prefilled_title_is_emptied_before_typing(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo mise en ligne")

    publish(tmp_path, monkeypatch, page)

    keys = [c[1] for c in page.calls if c[0] == "key"]
    assert keys[:2] == ["Control+A", "Backspace"]  # le champ est prerempli du nom du fichier : vider puis taper


def test_a_title_longer_than_100_characters_is_truncated(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo mise en ligne")

    publish(tmp_path, monkeypatch, page, clip=_clip(tmp_path, screen_title="x" * 150))

    assert page.title_typed() == "x" * 100


def test_shorts_is_not_added_twice_whatever_its_case(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo mise en ligne")

    publish(tmp_path, monkeypatch, page, clip=_clip(tmp_path, hashtags=["#jeu", "#shorts"]))

    assert page.description_typed().lower().count("#shorts") == 1


def test_shorts_is_added_when_there_is_no_hashtag(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo mise en ligne")

    publish(tmp_path, monkeypatch, page, clip=_clip(tmp_path, hashtags=[]))

    assert page.description_typed() == "Une légende\n\n#Shorts"


def test_made_for_kids_yes_clicks_the_yes_radio(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo mise en ligne")

    publish(tmp_path, monkeypatch, page, options={"made_for_kids": True})

    assert page.kids is True


def test_a_private_publication_saves_instead_of_publishing(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo enregistrée")

    result = publish(tmp_path, monkeypatch, page, options={"visibility": "private"})

    assert page.clicked("Privée") and page.clicked("Enregistrer") and not page.clicked("Publier")
    assert result["state"] == "published" and "en privé" in result["note"]


def test_an_unlisted_publication_picks_the_unlisted_radio_and_publishes(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo mise en ligne")

    result = publish(tmp_path, monkeypatch, page, options={"visibility": "unlisted"})

    assert page.clicked("Non répertoriée") and page.clicked("Publier")
    assert "non répertoriée" in result["note"]


def test_the_welcome_dialog_is_closed_with_continuer_before_the_upload(tmp_path, monkeypatch):
    page = FakeUpload(welcome=True, success_text="Vidéo mise en ligne")

    publish(tmp_path, monkeypatch, page)

    assert page.welcome is False and page.clicked("Continuer")
    order = [c[0] for c in page.calls]
    assert order.index("upload") > [i for i, c in enumerate(page.calls) if c == ("click", "Continuer")][0]


def test_the_publication_pauses_like_a_human_between_actions(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo mise en ligne")

    publish(tmp_path, monkeypatch, page, min_action_delay_s=0.5, max_action_delay_s=0.9)

    assert publish.sleeps and all(0.5 <= s <= 0.9 for s in publish.sleeps)


def test_the_worker_beat_is_called_while_driving(tmp_path, monkeypatch):
    ticks = []
    page = FakeUpload(success_text="Vidéo mise en ligne")
    monkeypatch.chdir(tmp_path)

    @contextmanager
    def opener(account, *, headless):
        yield FakeContext(page)

    config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "o")
    youtube.publish(_clip(tmp_path), "ma_chaine", mode="immediate", config=config, now=NOW, opener=opener,
                    sleep=lambda s: None, rng=random.Random(1), on_tick=lambda: ticks.append(1))
    assert ticks


def test_the_final_button_is_awaited_while_the_upload_is_still_running(tmp_path, monkeypatch):
    page = FakeUpload(upload_polls=3, success_text="Vidéo mise en ligne")

    result = publish(tmp_path, monkeypatch, page, upload_timeout_s=30)

    assert result["state"] == "published"
    assert [c for c in page.calls if c == ("click", "Publier")] == [("click", "Publier")]  # un seul clic, une fois actif


def test_a_final_button_that_stays_disabled_is_a_stop(tmp_path, monkeypatch):
    page = FakeUpload(upload_polls=999)

    stop = stop_of(tmp_path, monkeypatch, page, upload_timeout_s=2)

    assert stop.code == "element_missing" and "envoi" in stop.reason
    assert not page.clicked("Publier")


def test_the_success_window_is_awaited_after_the_final_click(tmp_path, monkeypatch):
    page = FakeUpload(success_polls=2, success_text="Vidéo mise en ligne")

    result = publish(tmp_path, monkeypatch, page, publish_confirm_timeout_s=30)

    assert result["state"] == "published"


def test_no_success_window_is_an_explicit_stop_never_a_supposed_success(tmp_path, monkeypatch):
    page = FakeUpload(success_text=None)

    stop = stop_of(tmp_path, monkeypatch, page, publish_confirm_timeout_s=2)

    assert stop.code == "publish_unconfirmed" and "à vérifier à la main" in stop.reason
    assert stop.capture is not None and Path(stop.capture).is_file()


def test_the_final_button_label_must_match_the_mode(tmp_path, monkeypatch):
    page = FakeUpload(final_label="Enregistrer", success_text="Vidéo mise en ligne")

    stop = stop_of(tmp_path, monkeypatch, page)

    assert stop.code == "unexpected_page" and "« Enregistrer » au lieu de « Publier »" in stop.reason
    assert not page.final_clicked


# -- publication programmee, heure de Paris (R8)


def test_a_scheduled_publication_types_paris_time_when_the_pc_is_on_london_time(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo programmée")
    at = datetime(2026, 10, 4, 20, 30, tzinfo=LONDON)  # le PC (Londres) pense 20:30 : Paris = 21:30

    result = publish(tmp_path, monkeypatch, page, mode="scheduled", schedule_at=at)

    assert (page.date_text, page.time_text) == ("4 oct. 2026", "21:30")
    assert page.tz_choice == "(UTC+02:00) Paris"  # fuseau choisi explicitement
    assert not page.clicked("(GMT+0100) Heure locale")
    assert result["state"] == "scheduled_on_youtube" and result["post_id"] == SHORT_ID
    assert datetime.fromisoformat(result["publish_at"]) == at
    assert ("press", "Enter") in page.calls


def test_the_scheduled_date_is_the_paris_date_not_the_pc_date(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo programmée")
    at = datetime(2026, 10, 4, 23, 30, tzinfo=LONDON)  # 00:30 le lendemain a Paris

    publish(tmp_path, monkeypatch, page, mode="scheduled", schedule_at=at)

    assert (page.date_text, page.time_text) == ("5 oct. 2026", "00:30")


def test_the_scheduled_time_in_utc_is_converted_to_paris(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo programmée")
    at = datetime(2026, 12, 25, 8, 5, tzinfo=timezone.utc)  # hiver : Paris = UTC+1

    monkeypatch.chdir(tmp_path)
    publish_now = datetime(2026, 12, 1, tzinfo=timezone.utc)

    @contextmanager
    def opener(account, *, headless):
        yield FakeContext(page)

    config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "o",
                    _sections={"youtube": {"schedule_max_days": 60, "publish_confirm_timeout_s": 3}})
    youtube.publish(_clip(tmp_path), "ma_chaine", mode="scheduled", schedule_at=at, config=config, now=publish_now,
                    opener=opener, sleep=lambda s: None, rng=random.Random(1))

    assert (page.date_text, page.time_text) == ("25 déc. 2026", "09:05")


def test_the_scheduled_final_button_is_programmer(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo programmée")

    publish(tmp_path, monkeypatch, page, mode="scheduled", schedule_at=NOW + timedelta(days=1))

    assert [c for c in page.calls if c == ("click", "Programmer")]
    assert not page.clicked("Publier") and not page.clicked("Publique")


def test_a_scheduled_date_that_the_page_did_not_take_is_a_stop_with_capture(tmp_path, monkeypatch):
    page = FakeUpload(ignore_date=True, success_text="Vidéo programmée")

    stop = stop_of(tmp_path, monkeypatch, page, mode="scheduled", schedule_at=NOW + timedelta(days=1))

    assert stop.code == "unexpected_page" and "programmation non prise en compte" in stop.reason
    assert stop.capture is not None and Path(stop.capture).is_file()
    assert not page.final_clicked  # jamais de clic final sur une relecture fausse


def test_a_date_dropdown_without_a_date_field_is_a_stop_with_capture(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Vidéo programmée")
    page.date_input_missing = True

    stop = stop_of(tmp_path, monkeypatch, page, mode="scheduled", schedule_at=NOW + timedelta(days=1))

    assert stop.code == "element_missing" and "date" in stop.reason
    assert not page.final_clicked


def test_a_timezone_that_the_page_did_not_take_is_a_stop(tmp_path, monkeypatch):
    page = FakeUpload(ignore_tz=True, success_text="Vidéo programmée")

    stop = stop_of(tmp_path, monkeypatch, page, mode="scheduled", schedule_at=NOW + timedelta(days=1))

    assert stop.code == "unexpected_page" and "fuseau" in stop.reason and "Paris" in stop.reason
    assert not page.final_clicked


def test_a_missing_paris_timezone_option_is_a_stop_never_the_local_time(tmp_path, monkeypatch):
    page = FakeUpload(drop_tz_paris=True, success_text="Vidéo programmée")

    stop = stop_of(tmp_path, monkeypatch, page, mode="scheduled", schedule_at=NOW + timedelta(days=1))

    assert stop.code == "element_missing" and "Paris" in stop.reason
    assert not page.clicked("(GMT+0100) Heure locale") and not page.final_clicked


def test_an_unknown_scheduled_success_title_is_a_stop_to_verify_in_real(tmp_path, monkeypatch):
    page = FakeUpload(success_text="Quelque chose d'autre")

    stop = stop_of(tmp_path, monkeypatch, page, mode="scheduled", schedule_at=NOW + timedelta(days=1),
                   publish_confirm_timeout_s=2)

    assert stop.code == "publish_unconfirmed"


@pytest.mark.parametrize("visibility", ["private", "unlisted"])
def test_a_scheduled_publication_is_public_only(tmp_path, monkeypatch, visibility):
    page = FakeUpload()
    with pytest.raises(youtube.YouTubeError, match="programm"):
        publish(tmp_path, monkeypatch, page, mode="scheduled", schedule_at=NOW + timedelta(days=1),
                options={"visibility": visibility})
    assert page.calls == []  # refuse avant tout navigateur


@pytest.mark.parametrize("at,match", [(None, "date avec fuseau"), (datetime(2026, 10, 5, 9, 0), "date avec fuseau"),
                                      (NOW + timedelta(minutes=2), "avance minimale"),
                                      (NOW + timedelta(days=4000), "limite")])
def test_a_bad_schedule_date_is_refused_before_any_browser(tmp_path, monkeypatch, at, match):
    page = FakeUpload()
    with pytest.raises(youtube.YouTubeError, match=match):
        publish(tmp_path, monkeypatch, page, mode="scheduled", schedule_at=at)
    assert page.calls == []


# -- arret sur (R3)


def test_a_captcha_stops_immediately_with_a_capture_and_the_reason(tmp_path, monkeypatch):
    page = FakeUpload(captcha=True)

    stop = stop_of(tmp_path, monkeypatch, page)

    assert stop.code == "captcha" and "captcha" in stop.reason
    assert Path(stop.capture).parent == browser.profile_dir("ma_chaine") / "captures" and Path(stop.capture).is_file()
    assert page.files == []  # rien n'est envoye


def test_a_google_login_page_on_studio_stops_with_the_login_code(tmp_path, monkeypatch):
    page = FakeUpload(studio_redirect=LOGIN_URL)

    stop = stop_of(tmp_path, monkeypatch, page)

    assert stop.code == "login" and "ma_chaine" in stop.reason
    assert Path(stop.capture).is_file() and page.files == []


def test_a_google_verification_page_stops_with_the_verification_code(tmp_path, monkeypatch):
    page = FakeUpload(studio_redirect="https://accounts.google.com/v3/signin/challenge/pwd")

    stop = stop_of(tmp_path, monkeypatch, page)

    assert stop.code == "verification" and "vérification" in stop.reason


def test_studio_that_does_not_open_a_channel_is_an_unexpected_page_stop(tmp_path, monkeypatch):
    page = FakeUpload(studio_redirect=f"{STUDIO}/")

    stop = stop_of(tmp_path, monkeypatch, page)

    assert stop.code == "unexpected_page" and "page inattendue" in stop.reason


def test_a_menu_without_the_upload_entry_is_an_unexpected_page_stop(tmp_path, monkeypatch):
    page = FakeUpload(menu_items=("Passer au direct", "Nouvelle playlist", "Nouveau podcast"))

    stop = stop_of(tmp_path, monkeypatch, page)

    assert stop.code == "unexpected_page" and "Importer des vidéos" in stop.reason and "Créer" in stop.reason
    assert page.files == [] and page.menu_clicked is None


def test_an_unknown_dialog_stops_instead_of_clicking_blindly(tmp_path, monkeypatch):
    page = FakeUpload(welcome=False)
    unknown = El(page, text="Une fenêtre qu'on ne connaît pas")
    original = page.query_selector_all

    def with_unknown(selector):
        found = original(selector)
        return [*found, unknown] if selector == _sel()["modal"]["container"] else found

    page.query_selector_all = with_unknown

    stop = stop_of(tmp_path, monkeypatch, page)

    assert stop.code == "unexpected_page" and "fenêtre inattendue" in stop.reason
    assert page.files == [] and not any(c[0] == "click" for c in page.calls)


def test_a_missing_shorts_link_is_a_stop_naming_the_element(tmp_path, monkeypatch):
    page = FakeUpload(link=False)

    stop = stop_of(tmp_path, monkeypatch, page, upload_timeout_s=2)

    assert stop.code == "element_missing" and "video_link" in stop.reason
    assert Path(stop.capture).is_file() and not page.final_clicked


def test_a_missing_description_box_is_a_stop(tmp_path, monkeypatch):
    page = FakeUpload(text_boxes=1)

    stop = stop_of(tmp_path, monkeypatch, page)

    assert stop.code == "element_missing" and not page.final_clicked


def test_an_unexpected_page_error_becomes_a_stop_with_capture(tmp_path, monkeypatch):
    page = FakeUpload()

    def boom(*args, **kwargs):
        raise RuntimeError("la page a planté")

    page.set_input_files = boom

    stop = stop_of(tmp_path, monkeypatch, page)

    assert stop.code == "unexpected_page" and "RuntimeError" in stop.reason and Path(stop.capture).is_file()


def test_a_failed_capture_is_said_in_the_stop_reason(tmp_path, monkeypatch):
    page = FakeUpload(captcha=True, screenshot_error="écran verrouillé")

    stop = stop_of(tmp_path, monkeypatch, page)

    assert stop.capture is None and "capture d'écran impossible : écran verrouillé" in stop.reason


# -- refus avant navigateur, reglages


def test_a_missing_account_mp4_or_title_is_an_explicit_error(tmp_path, monkeypatch):
    page = FakeUpload()
    monkeypatch.chdir(tmp_path)
    config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "o")
    ok = _clip(tmp_path)
    with pytest.raises(youtube.YouTubeError, match="compte YouTube manquant"):
        youtube.publish(ok, "", mode="immediate", config=config, now=NOW)
    with pytest.raises(youtube.YouTubeError, match="mp4 introuvable"):
        youtube.publish({**ok, "video_path": tmp_path / "nope.mp4"}, "ma_chaine", mode="immediate", config=config, now=NOW)
    with pytest.raises(youtube.YouTubeError, match="titre"):
        youtube.publish({**ok, "screen_title": ""}, "ma_chaine", mode="immediate", config=config, now=NOW)
    with pytest.raises(youtube.YouTubeError, match="mode de publication invalide"):
        youtube.publish(ok, "ma_chaine", mode="demain", config=config, now=NOW)
    assert page.calls == []


def test_post_options_are_validated_with_explicit_errors():
    base = youtube.get_settings(None)
    assert youtube.post_settings(base, {"visibility": "private", "made_for_kids": True, "title": "t"})["visibility"] == "private"
    with pytest.raises(youtube.YouTubeError, match="inconnu"):
        youtube.post_settings(base, {"couleur": "rouge"})
    with pytest.raises(youtube.YouTubeError, match="visibility"):
        youtube.post_settings(base, {"visibility": "secrète"})
    with pytest.raises(youtube.YouTubeError, match="made_for_kids"):
        youtube.post_settings(base, {"made_for_kids": "non"})
    with pytest.raises(youtube.YouTubeError, match="title"):
        youtube.post_settings(base, {"title": "   "})


def test_the_publication_settings_have_defaults_and_validation():
    d = youtube.CONFIG_DEFAULTS
    assert (d["visibility"], d["made_for_kids"], d["publish_mode"]) == ("public", False, "immediate")
    for key in ("upload_timeout_s", "publish_confirm_timeout_s", "schedule_max_days", "schedule_min_minutes"):
        assert key in d
    config = Config(mode="review", workspace_dir=Path("w"), output_dir=Path("o"), _sections={"youtube": {"visibility": "x"}})
    with pytest.raises(youtube.YouTubeError, match="visibility"):
        youtube.get_settings(config)
    config = Config(mode="review", workspace_dir=Path("w"), output_dir=Path("o"), _sections={"youtube": {"made_for_kids": "x"}})
    with pytest.raises(youtube.YouTubeError, match="made_for_kids"):
        youtube.get_settings(config)


def test_clip_payload_reads_the_sidecar_fields_and_refuses_a_broken_sidecar(tmp_path):
    sidecar = {"video_id": "aaaaaaaaaaa", "clip_id": "01", "caption": "c", "hashtags": ["#a"], "screen_title": "T"}
    payload = youtube.clip_payload(sidecar, tmp_path)
    assert payload == {"video_path": tmp_path / "aaaaaaaaaaa" / "01.mp4", "caption": "c", "hashtags": ["#a"],
                       "screen_title": "T"}
    with pytest.raises(youtube.YouTubeError, match="caption"):
        youtube.clip_payload({**sidecar, "caption": " "}, tmp_path)
    with pytest.raises(youtube.YouTubeError, match="hashtags"):
        youtube.clip_payload({**sidecar, "hashtags": None}, tmp_path)


@pytest.mark.parametrize("table,key", [("urls", "short"), ("expect", "short_id_pattern"),
                                       ("labels", "create"), ("labels", "upload_menu"), ("labels", "next"),
                                       ("labels", "final_schedule"), ("selectors", "menu_item"),
                                       ("selectors", "final_button"), ("selectors", "file_input"),
                                       ("success", "published"), ("detect", "captcha"), ("calendar", "months")])
def test_a_missing_publication_landmark_is_an_explicit_error_naming_the_key(tmp_path, table, key):
    text = SELECTORS.read_text(encoding="utf-8")
    broken = re.sub(rf"(?m)^{key} = ", f"{key}_absent = ", text, count=1)
    assert broken != text
    bad = tmp_path / "s.toml"
    bad.write_text(broken, encoding="utf-8")
    with pytest.raises(youtube.YouTubeError, match=rf"\[{table}\] {key}"):
        youtube.load_selectors(bad)


def test_the_unverified_landmarks_are_flagged_in_the_toml():
    text = SELECTORS.read_text(encoding="utf-8")
    # le repere du bouton final est ecrit sans releve reel : signale ; le lien du Short est releve
    assert "NON VÉRIFIÉ" in text[text.index("# Bouton final de la dialog"):text.index("final_button =")]
    assert "vérifié 2026-10-03" in text[text.index("# Lien youtube.com/shorts/<id> affiche"):text.index("video_link =")]


# ---------------------------------------------------------------- [browser] pilot_wait_s (TASK-2456, revue M5)


def test_the_pilot_wait_of_the_config_reaches_the_browser_for_publishing_and_login_check(tmp_path, monkeypatch):
    import time

    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(browser.CONFIG_DEFAULTS, "pilot_wait_s", 5)  # le defaut ne doit pas s'appliquer
    config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "output",
                    _sections={"browser": {"pilot_wait_s": 0.2}})
    clip = _clip(tmp_path)
    calls = {
        "publish": lambda: youtube.publish(clip, "ma_chaine", mode="immediate", config=config, now=NOW),
        "verify_login": lambda: youtube.verify_login("ma_chaine", config=config, now=NOW),
    }
    with browser._pilot_lock("compte_occupe", 1):  # un autre compte est en cours de pilotage
        for name, call in calls.items():
            started = time.monotonic()
            with pytest.raises(browser.BrowserError, match="compte_occupe"):
                call()
            assert time.monotonic() - started < 2, f"{name} : pilot_wait_s de config.toml ignoré"


# -- captures sous [browser] state_dir (TASK-89dc)


def _opener_on(page):
    @contextmanager
    def opener(account, *, headless):
        yield FakeContext(page)
    return opener


def test_a_publication_capture_follows_the_browser_state_dir_setting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / "profils"
    config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "o",
                    _sections={"youtube": {"upload_timeout_s": 3, "publish_confirm_timeout_s": 3, "poll_interval_s": 1,
                                           "action_timeout_s": 2},
                               "browser": {"state_dir": str(state)}})

    with pytest.raises(youtube.YouTubeStop) as caught:
        youtube.publish(_clip(tmp_path), "ma_chaine", mode="immediate", config=config, now=NOW,
                        opener=_opener_on(FakeUpload(captcha=True)), sleep=lambda s: None, rng=random.Random(1))

    assert Path(caught.value.capture).parent == state / "ma_chaine" / "captures" and Path(caught.value.capture).is_file()


def test_a_verification_capture_follows_the_browser_state_dir_setting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / "profils"
    config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "o",
                    _sections={"youtube": {"action_timeout_s": 2, "poll_interval_s": 1},
                               "browser": {"state_dir": str(state)}})
    page = FakeStudio(redirect="https://www.youtube.com/", nav_text=None)

    result = youtube.verify_login("ma_chaine", config=config, now=NOW, opener=_opener_on(page),
                                  sleep=lambda s: None, rng=random.Random(1))

    assert result["capture"] and Path(result["capture"]).parent == state / "ma_chaine" / "captures"
