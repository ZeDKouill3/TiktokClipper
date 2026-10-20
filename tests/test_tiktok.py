"""TASK-0b78 : clipper.tiktok (SPEC-9225 R3-R6, R9, ADR-1a58).

Playwright n'est jamais lance : une fausse page (objets simules) rejoue les cas
succes, captcha, verification, connexion expiree, element absent, page
inattendue. Aucun navigateur, aucun reseau, aucun TikTok.
"""

from __future__ import annotations

import re
import ast
import copy
import json
import random
import os
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from clipper import browser, tiktok
from clipper.config import Config

ROOT = Path(__file__).resolve().parent.parent
SELECTORS = ROOT / "clipper" / "assets" / "tiktok_selectors.toml"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
LINK = "https://www.tiktok.com/@ma_chaine/video/7300000000000000001"
PARIS = ZoneInfo("Europe/Paris")


_SELECTORS: dict | None = None


def _sel() -> dict:
    """Copie profonde des selecteurs, parses une seule fois : chaque appel
    reste independant (un test qui modifie sa copie n'en touche aucune autre)."""
    global _SELECTORS
    if _SELECTORS is None:
        _SELECTORS = tiktok.load_selectors()
    return copy.deepcopy(_SELECTORS)


# ---------------------------------------------------------------- fausse page


def _css_matches(selector, attrs):
    """Rejoue le SELECTEUR REEL sur les attributs d'une fenetre (liste separee par des virgules ; chaque terme :
    ``[attr='v']`` / ``.classe`` hors ``:not(...)``) : un faux qui ignore le selecteur ne peut pas voir qu'il rate
    une vraie fenetre (TASK-4b5a, TUXModal sans role)."""
    for part in re.split(r",\s*(?![^()]*\))", selector):
        part = re.sub(r":not\((?:[^()]|\([^()]*\))*\)", "", part)
        wanted = re.findall(r"\[([\w-]+)='([^']*)'\]", part), re.findall(r"\.([\w-]+)", part)
        classes = attrs.get("class", "").split()
        if (wanted[0] or wanted[1]) and all(attrs.get(k) == v for k, v in wanted[0]) and all(c in classes for c in wanted[1]):
            return True
    return False


class FakeElement:
    owner = None   # fenetre dont l'element fait partie (None : la page elle-meme)

    def __init__(self, page, selector, href=None, text="", value=None, on_click=None, visible=True):
        self.page, self.selector, self.href = page, selector, href
        self.text, self.value, self.on_click, self.visible = text, value, on_click, visible
        self.children: dict[str, "FakeElement"] = {}

    def click(self, **kwargs):
        self.page.calls.append(("click", self.selector))
        self.page.click_timeouts.append(kwargs.get("timeout"))
        if self.page.fail_click:
            raise RuntimeError("Target page, context or browser has been closed")
        rank = self.page.modals.index(self.owner) if self.owner in self.page.modals else -1
        if self.page.modals_block_clicks and any(self.page.modals[rank + 1:]):
            # Playwright : l'overlay d'une fenetre PLUS HAUTE recouvre l'element et attend en vain (TimeoutError)
            raise TimeoutError("Locator.click: Timeout 10000ms exceeded.\n  - <div class=\"TUXModal-overlay\"> "
                               "intercepts pointer events\n  - retrying click action")
        for added in self.page.after_click.get(self.selector, ()):
            self.page.present.add(added)
        if self.on_click is not None:
            self.on_click()

    def fill(self, text, **kwargs):
        self.page.calls.append(("fill", self.selector, text))

    def get_attribute(self, name):
        return self.href if name == "href" else None

    def inner_text(self):
        return self.text() if callable(self.text) else self.text

    def input_value(self):
        return self.value() if callable(self.value) else self.value

    def is_visible(self):
        return self.visible and self.selector not in self.page.hidden

    def query_selector(self, selector):
        return self.children.get(selector)


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key, **kwargs):
        self.page.calls.append(("press", key))

    def type(self, text, **kwargs):
        self.page.calls.append(("type", text))

    def insert_text(self, text):
        self.page.calls.append(("type", text))


class FakeModal(FakeElement):
    """Une fenetre surgissante : un bouton par libelle ; le clic la ferme."""

    def __init__(self, page, text, labels, attrs=None):
        super().__init__(page, _sel()["modal"]["container"], text=text)
        self.attrs = attrs if attrs is not None else {"role": "dialog"}   # ce que le DOM expose au selecteur
        self.labels = list(labels)
        template = _sel()["modal"]["button"]
        for label in labels:
            button = FakeElement(page, template.format(label=label), on_click=lambda label=label: self.close(label))
            button.owner = self
            self.children[template.format(label=label)] = button

    def close(self, label):
        self.page.modals.remove(self)
        self.page.popups_closed.append(label)


class FakePage:
    """``present`` : selecteurs actuellement affiches ; ``redirect`` : adresse
    reelle apres goto ; ``after_click`` : selecteur clique -> selecteurs qui apparaissent ;
    ``texts`` / ``lists`` : texte d'un selecteur, elements d'un query_selector_all ;
    ``timeline`` : une fonction par ``wait_for_timeout`` (l'etat de la page evolue)."""

    def __init__(self, present, *, redirect=None, link=LINK, screenshot_error=None):
        self.present = set(present)
        self.redirect, self.link, self.screenshot_error = redirect, link, screenshot_error
        self.after_click: dict[str, list[str]] = {}
        self.texts: dict[str, str] = {}
        self.lists: dict[str, list[FakeElement]] = {}
        self.hidden: set[str] = set()
        self.modals: list[FakeModal] = []
        self.popups_closed: list[str] = []
        self.modals_block_clicks = False
        self.click_timeouts: list = []
        self.timeline: list = []
        self.waits = 0
        self.keyboard = FakeKeyboard(self)
        self.fail_click = False
        self.url = "about:blank"
        self.calls: list[tuple] = []
        self.html, self.content_error = "<html><body>page</body></html>", None

    def content(self):
        if self.content_error:
            raise RuntimeError(self.content_error)
        return self.html

    def goto(self, url, **kwargs):
        self.calls.append(("goto", url))
        self.url = self.redirect or url

    def make(self, selector):
        return FakeElement(self, selector, href=self.link, text=self.texts.get(selector, ""))

    def query_selector(self, selector):
        return self.make(selector) if selector in self.present else None

    def query_selector_all(self, selector):
        if selector == _sel()["modal"]["container"]:
            return [m for m in self.modals if _css_matches(selector, m.attrs)]
        return list(self.lists.get(selector, []))

    def wait_for_selector(self, selector, timeout=None, state=None):
        self.calls.append(("wait", selector))
        if selector not in self.present:
            raise TimeoutError(f"Timeout {timeout}ms exceeded waiting for {selector}")
        return self.make(selector)

    def wait_for_timeout(self, ms):
        self.calls.append(("poll", ms))
        step = self.timeline[self.waits] if self.waits < len(self.timeline) else None
        self.waits += 1
        if step is not None:
            step()

    def set_input_files(self, selector, path, **kwargs):
        self.calls.append(("upload", selector, str(path)))

    def screenshot(self, path=None, **kwargs):
        if self.screenshot_error:
            raise RuntimeError(self.screenshot_error)
        Path(path).write_bytes(b"\x89PNG fake")

    def clicks(self):
        return [c[1] for c in self.calls if c[0] == "click"]

    def fills(self):
        return [c for c in self.calls if c[0] == "fill"]

    def typed(self):
        return [c[1] for c in self.calls if c[0] == "type"]


class FakeContext:
    def __init__(self, page):
        self.page, self.pages = page, [page]

    def new_page(self):
        return self.page


MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
          "novembre", "décembre"]


class StudioPage(FakePage):
    """TikTok Studio simule : repliement des parametres, visibilite, Maintenant/Programmer, calendrier
    (mois navigable par fleches), selecteur d'heure, verification de contenu, bouton final unique."""

    def __init__(self, *, folded=False, calendar=(2026, 10), minute_step=5, check="ok", confirm=("navigation",),
                 content_links=None, **kwargs):
        super().__init__(set(), **kwargs)
        sel = _sel()["selectors"]
        self.s = sel
        self.confirm = list(confirm)  # une issue par clic sur le bouton final : navigation | message | none | dialog
        self.content_links = [("Ma legende #un #deux", LINK)] if content_links is None else list(content_links)
        self.cal = list(calendar)
        self.time_value, self.date_value = "10:00", "2026-10-01"
        self.mode = "now"
        self.posted: list[str] = []
        self.arrows: list[str] = []
        self.present |= {sel[k] for k in ("file_input", "upload_done", "caption_editor", "advanced_settings",
                                          "visibility_public", "visibility_private", "visibility_friends", "schedule_now",
                                          "schedule_later", "post_button", "discard_button", "schedule_picker_close")}
        if folded:
            self.present.add(sel["advanced_settings"])
            self.after_click[sel["advanced_settings"]] = [sel["visibility_dropdown"]]
        else:
            self.present.add(sel["visibility_dropdown"])
        self.set_check(check)
        # Interrupteur « Vérification de contenu simple » : allumé au départ (état du compte) ; ``switch`` garde son
        # état réel, ``refuse_switch`` : TikTok refuse le changement (l'interrupteur reste éteint).
        self.switch = {"checked": True, "disabled": False}
        self.refuse_switch = False
        self.present.add(sel["content_check_switch"])
        # Reglages par post (apres « Afficher plus ») : etat initial de TikTok (commentaires et reutilisation
        # cochees, contenu IA coupe) ; ``toggles`` garde chaque changement d'etat, jamais un clic « de position ».
        self.options = {"comment_switch": True, "reuse_switch": True, "ai_switch": False}
        self.disabled_options: set[str] = set()
        self.toggles: list[tuple[str, bool]] = []
        self.present |= {sel[k] for k in self.options}
        # Programmer / Maintenant : le texte du bouton final suit
        self.on_click = {sel["schedule_later"]: self.choose_scheduled, sel["schedule_now"]: self.choose_now}
        self.texts[sel["post_button"]] = _sel()["labels"]["post_now"]
        self.time_el = FakeElement(self, sel["schedule_inputs"], value=lambda: self.time_value,
                                   on_click=lambda: self.open("time"))
        self.date_el = FakeElement(self, sel["schedule_inputs"], value=lambda: self.date_value,
                                   on_click=lambda: self.open("date"))
        self.minute_step = minute_step

    # -- verification de contenu
    def set_check(self, check):
        sel = self.s
        for key in ("content_check_running", "content_check_ok", "content_check_problem"):
            self.present.discard(sel[key])
        self.present.add({"ok": sel["content_check_ok"], "running": sel["content_check_running"],
                          "problem": sel["content_check_problem"]}[check])

    # -- elements
    def make(self, selector):
        sel = self.s
        if selector == sel["post_button"]:
            return FakeElement(self, selector, text=self.texts[selector], on_click=self.after_post)
        if selector == sel["content_check_switch"]:
            return ContentSwitch(self, selector)
        if selector == sel["calendar_month_title"]:
            return FakeElement(self, selector, text=lambda: MONTHS[self.cal[1] - 1])
        if selector == sel["calendar_year_title"]:
            return FakeElement(self, selector, text=lambda: str(self.cal[0]))
        if selector == sel["schedule_picker_close"]:
            return FakeElement(self, selector, on_click=self.close_pickers)
        for name in self.options:
            if selector == sel[name]:
                return FakeToggle(self, selector, name)
        element = super().make(selector)
        element.on_click = self.on_click.get(selector)
        return element

    def after_post(self):
        self.posted.append(self.mode)
        outcome = self.confirm.pop(0) if self.confirm else "none"
        if outcome == "navigation":
            self.url = _sel()["expect"]["published_url_prefix"]
        elif outcome == "message":
            self.present.add(self.s["published_marker"])
        elif outcome in ("dialog", "dialog_recheck"):
            if outcome == "dialog_recheck":  # la verification de contenu repart apres l'annulation
                self.set_check("running")
            self.modals.append(FakeModal(self, "Continuer à publier ? Nous sommes encore en train de vérifier…",
                                         ["Annuler", "Publier maintenant"]))

    def evaluate(self, script):
        self.calls.append(("evaluate", script))
        return True  # liste courte : tout tient dans la fenetre, le bas est deja atteint

    def wait_for_selector(self, selector, timeout=None, state=None):
        if selector == _sel()["stats"]["post_link"] and self.url.startswith(_sel()["expect"]["published_url_prefix"]) \
                and self.content_links:
            self.calls.append(("wait", selector))
            return FakeElement(self, selector)
        return super().wait_for_selector(selector, timeout=timeout, state=state)

    def choose_scheduled(self):
        self.mode = "scheduled"
        self.texts[self.s["post_button"]] = _sel()["labels"]["post_scheduled"]
        self.present.add(self.s["schedule_inputs"])

    def choose_now(self):
        self.mode = "now"
        self.texts[self.s["post_button"]] = _sel()["labels"]["post_now"]

    def open(self, which):
        sel = self.s
        names = (("calendar_month_title", "calendar_year_title", "calendar_arrow", "calendar_day") if which == "date"
                 else ("timepicker_hour", "timepicker_minute"))
        self.present |= {sel[k] for k in names}

    def close_pickers(self):
        sel = self.s
        for key in ("calendar_month_title", "calendar_year_title", "calendar_arrow", "calendar_day",
                    "timepicker_hour", "timepicker_minute"):
            self.present.discard(sel[key])

    def query_selector_all(self, selector):
        sel = self.s
        if selector == _sel()["stats"]["post_link"]:
            if not self.url.startswith(_sel()["expect"]["published_url_prefix"]):
                return []
            return [FakeElement(self, selector, href=href, text=text) for text, href in self.content_links]
        if selector == sel["schedule_inputs"]:
            return [self.time_el, self.date_el] if selector in self.present else []
        if selector == sel["calendar_arrow"] and selector in self.present:
            return [FakeElement(self, selector, on_click=lambda: self.arrow(-1)),
                    FakeElement(self, selector, on_click=lambda: self.arrow(+1))]
        if selector == sel["calendar_day"] and selector in self.present:
            return [FakeElement(self, selector, text=str(d), on_click=lambda d=d: self.pick_day(d))
                    for d in range(1, 31)]
        if selector == sel["timepicker_hour"] and selector in self.present:
            return [FakeElement(self, selector, text=f"{h:02d}", on_click=lambda h=h: self.pick_time(hour=h))
                    for h in range(24)]
        if selector == sel["timepicker_minute"] and selector in self.present:
            return [FakeElement(self, selector, text=f"{m:02d}", on_click=lambda m=m: self.pick_time(minute=m))
                    for m in range(0, 60, self.minute_step)]
        return super().query_selector_all(selector)

    def arrow(self, delta):
        self.arrows.append("next" if delta > 0 else "prev")
        index = self.cal[0] * 12 + self.cal[1] - 1 + delta
        self.cal = [index // 12, index % 12 + 1]

    def pick_day(self, day):
        self.date_value = f"{self.cal[0]}-{self.cal[1]:02d}-{day:02d}"

    def pick_time(self, hour=None, minute=None):
        h, m = self.time_value.split(":")
        self.time_value = f"{hour if hour is not None else int(h):02d}:{minute if minute is not None else int(m):02d}"


class ContentSwitch(FakeElement):
    """L'interrupteur « Vérification de contenu simple » de la page : état dans ``page.switch``, ``check`` refusé si
    ``page.refuse_switch``. Chaque clic est journalisé dans ``calls``."""

    def is_checked(self):
        return self.page.switch["checked"]

    def is_disabled(self):
        return self.page.switch["disabled"]

    def check(self, **kwargs):
        self.page.calls.append(("check", self.selector))
        if not self.page.refuse_switch:
            self.page.switch["checked"] = True

    def uncheck(self, **kwargs):
        self.page.calls.append(("uncheck", self.selector))
        self.page.switch["checked"] = False


class FakeToggle(FakeElement):
    """Une case ou un interrupteur du bloc des reglages : ``check`` / ``uncheck`` changent l'etat de la page."""

    def __init__(self, page, selector, name):
        super().__init__(page, selector)
        self.name = name

    def is_checked(self):
        return self.page.options[self.name]

    def check(self, **kwargs):
        self.page.options[self.name] = True
        self.page.toggles.append((self.name, True))

    def is_enabled(self):
        return self.name not in self.page.disabled_options

    def evaluate(self, script):
        # clic JavaScript sur l'input (case dessinee en CSS) : bascule l'etat comme un clic utilisateur
        assert "click()" in script
        wanted = not self.page.options[self.name]
        self.page.options[self.name] = wanted
        self.page.toggles.append((self.name, wanted))

    def uncheck(self, **kwargs):
        self.page.options[self.name] = False
        self.page.toggles.append((self.name, False))


class Env:
    """Une publication complete contre un TikTok Studio simule."""

    def __init__(self, tmp_path, monkeypatch, *, remove=(), detect=None, page_kwargs=None, settings=None):
        monkeypatch.chdir(tmp_path)
        self.page = StudioPage(**(page_kwargs or {}))
        for name in remove:
            self.page.present.discard(_sel()["selectors"][name])
            self.page.after_click.pop(_sel()["selectors"][name], None)
        for kind in (detect or ()):
            self.page.present.add(_sel()["detect"][kind][0])
        self.opened: list[tuple] = []
        self.sleeps: list[float] = []
        self.ticks = 0
        self.mp4 = tmp_path / "output" / "aaaaaaaaaaa" / "01.mp4"
        self.mp4.parent.mkdir(parents=True, exist_ok=True)
        self.mp4.write_bytes(b"mp4")
        self.config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "output",
                             _sections={"tiktok": {"content_check": "wait", **(settings or {})}})
        self.clip = {"video_path": self.mp4, "caption": "Ma legende", "hashtags": ["#un", "#deux"]}

    @contextmanager
    def opener(self, account, *, headless):
        self.opened.append((account, headless))
        yield FakeContext(self.page)

    def publish(self, mode="immediate", schedule_at=None, **kwargs):
        return tiktok.publish(
            self.clip, "ma_chaine", mode=mode, schedule_at=schedule_at, config=self.config, now=NOW,
            opener=self.opener, sleep=self.sleeps.append, rng=random.Random(1),
            on_tick=lambda: setattr(self, "ticks", self.ticks + 1), **kwargs)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# ---------------------------------------------------------------- (1) interface, backend, defauts


def test_tiktok_defaults_are_the_new_account_values_of_the_cadence_study():
    d = tiktok.CONFIG_DEFAULTS
    assert d["backend"] == "browser"
    assert (d["max_posts_per_day"], d["min_gap_minutes"]) == (1, 480)
    assert (d["min_action_delay_s"], d["max_action_delay_s"]) == (0.3, 1)  # rapide (choix utilisateur)
    assert d["schedule_max_days"] == 10


def test_tiktok_section_is_a_valid_config_table(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('mode = "review"\n[tiktok]\nmax_posts_per_day = 3\n', encoding="utf-8")
    from clipper.config import load_config

    assert load_config(cfg).section("tiktok")["max_posts_per_day"] == 3


def test_api_backend_is_an_explicit_not_yet_available_error(env):
    env.config = Config(mode="review", workspace_dir=Path("w"), output_dir=Path("o"),
                        _sections={"tiktok": {"backend": "api"}})
    with pytest.raises(tiktok.TikTokError, match="pas encore disponible"):
        env.publish()
    with pytest.raises(tiktok.TikTokError, match="pas encore disponible"):
        tiktok.fetch_stats("ma_chaine", config=env.config)
    assert env.opened == []


def test_unknown_backend_is_refused(env):
    env.config = Config(mode="review", workspace_dir=Path("w"), output_dir=Path("o"),
                        _sections={"tiktok": {"backend": "magie"}})
    with pytest.raises(tiktok.TikTokError, match="backend"):
        env.publish()


# ---------------------------------------------------------------- (2) publication


def test_immediate_publish_uploads_mp4_with_caption_and_hashtags_and_returns_the_post(env):
    result = env.publish()

    sel = _sel()["selectors"]
    assert env.opened == [("ma_chaine", False)]  # navigateur visible (ADR-1a58)
    assert env.page.calls[0] == ("goto", _sel()["urls"]["upload"])
    assert ("upload", sel["file_input"], str(env.mp4)) in env.page.calls
    assert ("wait", sel["upload_done"]) in env.page.calls  # fin d'envoi : conteneur « Importé »
    assert env.page.clicks() == [sel["caption_editor"], sel["visibility_dropdown"], sel["visibility_public"],
                                 sel["schedule_now"], sel["post_button"]]
    assert env.page.posted == ["now"]  # un seul bouton final
    assert result == {"post_url": LINK, "post_id": "7300000000000000001", "state": "published",
                      "publish_at": NOW.isoformat(), "note": None}


def test_the_caption_is_cleared_then_inserted_at_once_never_filled(env):
    env.publish()

    keys = [c for c in env.page.calls if c[0] in ("press", "type")]
    text = "Ma legende #un #deux"
    assert keys[:2] == [("press", "Control+A"), ("press", "Backspace")]  # pre-rempli du nom du fichier : vide
    assert keys[2:] == [("type", text), ("press", "Escape")]  # insert_text instantane, puis Echap ferme les suggestions de hashtags
    assert env.page.fills() == []  # pas de fill sur l'editeur Draft.js
    first_click = env.page.calls.index(("click", _sel()["selectors"]["caption_editor"]))
    assert first_click < env.page.calls.index(("press", "Control+A"))


def test_private_visibility_selects_the_private_option(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, settings={"visibility": "private"})
    env.publish()
    sel = _sel()["selectors"]
    assert env.page.clicks() == [sel["caption_editor"], sel["visibility_dropdown"], sel["visibility_private"],
                                 sel["schedule_now"], sel["post_button"]]


def test_folded_settings_are_expanded_with_show_more_only_when_needed(tmp_path, monkeypatch):
    sel = _sel()["selectors"]
    folded = Env(tmp_path, monkeypatch, page_kwargs={"folded": True})
    folded.publish()
    assert folded.page.clicks()[:2] == [sel["caption_editor"], sel["advanced_settings"]]

    open_ = Env(tmp_path, monkeypatch)
    open_.publish()
    assert sel["advanced_settings"] not in open_.page.clicks()


def test_visibility_options_are_targeted_by_option_id_with_the_text_as_fallback():
    sel = _sel()["selectors"]
    for key, option_id, text in (("visibility_public", '0', "Tout le monde"), ("visibility_private", '1', "Toi uniquement")):
        assert f"option-\"{option_id}\"" in sel[key] and text in sel[key]
    assert "video_visibility_container" in sel["visibility_dropdown"] and "combobox" in sel["visibility_dropdown"]


def test_scheduled_publish_sets_the_time_and_date_through_the_pickers(env):
    when = (NOW + timedelta(days=2)).astimezone(PARIS).replace(hour=15, minute=30, second=0, microsecond=0)
    result = env.publish("scheduled", when)

    sel = _sel()["selectors"]
    assert env.page.clicks() == [sel["caption_editor"], sel["visibility_dropdown"], sel["visibility_public"],
                                 sel["schedule_later"], sel["schedule_inputs"], sel["calendar_day"],
                                 sel["schedule_picker_close"], sel["schedule_inputs"], sel["timepicker_hour"],
                                 sel["timepicker_minute"], sel["schedule_picker_close"], sel["post_button"]]
    assert env.page.fills() == []
    assert (env.page.date_value, env.page.time_value) == (when.strftime("%Y-%m-%d"), "15:30")
    assert env.page.posted == ["scheduled"]  # le meme bouton, devenu « Programmer »
    assert env.page.arrows == []  # meme mois : aucune fleche
    assert result["state"] == "scheduled_on_tiktok"
    assert result["publish_at"] == when.isoformat() and result["note"] is None


@pytest.fixture
def pc_in_london(monkeypatch):
    """Le PC de l'utilisateur est regle sur Londres (SPEC-5e50 R8) : TZ simule quand l'OS sait le faire."""
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/London")
    if hasattr(time, "tzset"):
        time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    if hasattr(time, "tzset"):
        time.tzset()


@pytest.mark.parametrize("target, expected_date, expected_time", [
    (datetime(2026, 10, 5, 15, 30, tzinfo=ZoneInfo("Europe/London")), "2026-10-05", "16:30"),   # 14:30 UTC -> Paris +2
    (datetime(2026, 10, 5, 23, 30, tzinfo=timezone.utc), "2026-10-06", "01:30"),                # Paris passe minuit
    (datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc), "2026-10-05", "14:00"),
])
def test_the_scheduled_date_typed_in_the_page_is_paris_time_whatever_the_pc_timezone(
        tmp_path, monkeypatch, pc_in_london, target, expected_date, expected_time):
    env = Env(tmp_path, monkeypatch)
    result = env.publish("scheduled", target)

    assert (env.page.date_value, env.page.time_value) == (expected_date, expected_time)
    assert datetime.fromisoformat(result["publish_at"]) == target  # meme instant


def test_no_page_date_is_taken_from_the_pc_timezone():
    source = Path(tiktok.__file__).read_text(encoding="utf-8")
    assert ".astimezone()" not in source  # fuseau du PC jamais utilise (R8)


@pytest.mark.parametrize("start, target_days, arrows", [
    ((2026, 10), 35, ["next"]),                 # 5 novembre : un mois plus tard
    ((2027, 1), 35, ["prev", "prev"]),          # le calendrier affiche janvier 2027 : deux fleches arriere
    ((2026, 11), 35, []),
])
def test_scheduled_publish_navigates_months_with_the_arrows_to_the_target_month(tmp_path, monkeypatch, start, target_days, arrows):
    env = Env(tmp_path, monkeypatch, page_kwargs={"calendar": start}, settings={"schedule_max_days": 40})
    when = (NOW + timedelta(days=target_days)).astimezone(PARIS).replace(hour=9, minute=15, second=0, microsecond=0)

    env.publish("scheduled", when)

    assert env.page.arrows == arrows
    assert env.page.date_value == when.strftime("%Y-%m-%d")
    assert env.page.time_value == "09:15"


def test_scheduled_minutes_are_rounded_to_the_step_offered_by_tiktok_and_logged(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch, page_kwargs={"minute_step": 15})
    when = (NOW + timedelta(days=2)).astimezone(PARIS).replace(hour=12, minute=7, second=0, microsecond=0)

    with caplog.at_level("WARNING"):
        result = env.publish("scheduled", when)

    assert env.page.time_value == "12:00"
    assert "07" in caplog.text and "00" in caplog.text and "arrondi" in caplog.text
    assert result["publish_at"] == when.replace(minute=0).isoformat()  # l'instant reellement programme
    assert "arrondi" in result["note"]


def test_a_hidden_or_wrong_schedule_field_is_an_unexpected_page_stop_not_a_guess(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.page.time_value = "pas une heure"  # le premier champ n'est pas l'heure (« : » attendu)
    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish("scheduled", NOW + timedelta(days=2))
    assert stop.value.code == "unexpected_page" and "heure" in str(stop.value)
    assert env.page.posted == []


def test_the_final_button_label_must_match_the_mode(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.page.on_click[_sel()["selectors"]["schedule_now"]] = lambda: env.page.texts.__setitem__(
        _sel()["selectors"]["post_button"], "Programmer")  # la page est restee sur « Programmer »
    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()
    assert stop.value.code == "unexpected_page" and "Publier" in str(stop.value)
    assert env.page.posted == []


def test_scheduled_beyond_schedule_max_days_is_refused_before_opening_the_browser(env):
    with pytest.raises(tiktok.TikTokError, match="10 jours"):
        env.publish("scheduled", NOW + timedelta(days=10, minutes=1))
    assert env.opened == []


def test_scheduled_without_a_date_or_too_close_is_refused(env):
    with pytest.raises(tiktok.TikTokError, match="date"):
        env.publish("scheduled", None)
    with pytest.raises(tiktok.TikTokError, match="minutes"):
        env.publish("scheduled", NOW + timedelta(minutes=5))
    assert env.opened == []


def test_unknown_mode_missing_mp4_and_empty_account_are_refused(env):
    with pytest.raises(tiktok.TikTokError, match="mode"):
        env.publish("demain")
    env.mp4.unlink()
    with pytest.raises(tiktok.TikTokError, match="mp4"):
        env.publish()
    with pytest.raises(tiktok.TikTokError, match="compte"):
        tiktok.publish(env.clip, "", mode="immediate", config=env.config, opener=env.opener)


# ---------------------------------------------------------------- TASK-e4bf : preuve de publication, lien du post


def _published_prefix() -> str:
    return _sel()["expect"]["published_url_prefix"]


def test_a_private_post_cannot_be_scheduled_and_is_refused_before_any_browser_is_opened(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, settings={"visibility": "private"})
    when = NOW + timedelta(days=2)

    with pytest.raises(tiktok.TikTokError, match="privée") as refused:
        env.publish("scheduled", when)

    assert "programm" in str(refused.value) and "visibility" in str(refused.value)
    assert env.opened == [] and env.page.calls == []  # aucun navigateur ouvert, aucune page touchee
    # le mode immediat reste possible pour un post prive
    assert env.publish("immediate")["state"] == "published"


def test_publication_is_proved_by_the_navigation_to_the_content_page(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"confirm": ["navigation"]})

    result = env.publish()

    assert env.page.url.startswith(_published_prefix())
    assert _sel()["selectors"]["published_marker"] not in env.page.present  # aucun message : la navigation suffit
    assert result["state"] == "published" and result["post_url"] == LINK


def test_publication_is_proved_by_the_published_message_when_the_url_does_not_change(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"confirm": ["message"]})

    result = env.publish()

    assert not env.page.url.startswith(_published_prefix()) or ("goto", _sel()["urls"]["stats"]) in env.page.calls
    assert result["state"] == "published"
    assert ("goto", _sel()["urls"]["stats"]) in env.page.calls  # le lien est cherche sur la page Publications
    assert result["post_url"] == LINK


def test_no_sign_of_publication_is_an_r4_failure_with_a_capture_never_an_assumed_success(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"confirm": ["none"]},
              settings={"publish_confirm_timeout_s": 10, "poll_interval_s": 5})

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "publish_unconfirmed"
    assert "10 s" in str(stop.value) and "publish_confirm_timeout_s" in str(stop.value)
    assert stop.value.capture is not None and stop.value.capture.is_file()
    assert env.page.posted == ["now"]  # le bouton a ete clique une fois, jamais re-clique a l'aveugle
    assert [c for c in env.page.calls if c[0] == "poll"] == [("poll", 5000.0), ("poll", 5000.0)]


def test_publish_confirm_timeout_has_a_default_and_is_validated(tmp_path):
    assert tiktok.CONFIG_DEFAULTS["publish_confirm_timeout_s"] >= 1
    config = Config(mode="review", workspace_dir=tmp_path, output_dir=tmp_path,
                    _sections={"tiktok": {"publish_confirm_timeout_s": 0}})
    with pytest.raises(tiktok.TikTokError, match="publish_confirm_timeout_s"):
        tiktok.get_settings(config)


def test_the_continue_publishing_window_is_cancelled_then_the_post_is_retried_once(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch, page_kwargs={"confirm": ["dialog", "navigation"]})
    sel = _sel()

    with caplog.at_level("INFO"):
        result = env.publish()

    cancel = sel["modal"]["button"].format(label="Annuler")
    clicks = env.page.clicks()
    assert clicks[-3:] == [sel["selectors"]["post_button"], cancel, sel["selectors"]["post_button"]]
    assert env.page.popups_closed == ["Annuler"] and env.page.modals == []
    assert env.page.posted == ["now", "now"]  # un nouvel essai, une fois
    assert "Continuer à publier" in caplog.text
    assert result["state"] == "published" and result["post_url"] == LINK


def test_the_continue_publishing_window_waits_for_the_content_check_before_retrying(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"confirm": ["dialog_recheck", "navigation"]})
    env.page.timeline = [lambda: None, lambda: env.page.set_check("ok")]  # « en cours » deux lectures, puis OK

    env.publish()

    polls = [c for c in env.page.calls if c[0] == "poll"]
    assert len(polls) >= 2  # il a attendu la fin de la verification avant le nouvel essai
    assert env.page.posted == ["now", "now"]
    last_poll = max(i for i, c in enumerate(env.page.calls) if c[0] == "poll")
    second_post = [i for i, c in enumerate(env.page.calls) if c == ("click", env.page.s["post_button"])][1]
    assert last_poll < second_post


def test_the_continue_publishing_window_twice_is_an_r4_failure(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"confirm": ["dialog", "dialog"]})

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "publish_unconfirmed"
    assert "Continuer à publier" in str(stop.value) and stop.value.capture is not None
    assert env.page.posted == ["now", "now"]  # un seul nouvel essai


def test_the_post_link_is_the_first_video_link_whose_text_starts_the_published_caption(tmp_path, monkeypatch):
    other = "https://www.tiktok.com/@ma_chaine/video/7299999999999999999"
    mine = "/@ma_chaine/video/7300000000000000042?lang=fr"
    env = Env(tmp_path, monkeypatch, page_kwargs={"content_links": [
        ("Une autre legende", other), ("Ma legende #un", mine), ("Ma legende #un #deux", LINK)]})

    result = env.publish()

    # texte tronque = debut de la legende publiee ; href relatif complete par l'adresse de la page
    assert result["post_url"] == "https://www.tiktok.com/@ma_chaine/video/7300000000000000042?lang=fr"
    assert result["post_id"] == "7300000000000000042"  # fin du href
    assert result["note"] is None and result["state"] == "published"


def test_a_post_link_not_found_is_a_published_post_with_an_explicit_missing_link_note(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"content_links": [
        ("Une autre legende", "https://www.tiktok.com/@ma_chaine/video/7299999999999999999")]})

    result = env.publish()

    assert result["state"] == "published"  # la publication a reussi
    assert result["post_url"] is None and result["post_id"] is None  # pas d'id invente
    assert "lien" in result["note"] and "introuvable" in result["note"]


def test_a_post_link_lookup_that_breaks_never_turns_a_proved_publication_into_a_failure(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"content_links": []})  # la page ne montre aucun lien

    result = env.publish()

    assert result["state"] == "published" and result["post_url"] is None and "lien" in result["note"]


OTHER_LINKS = [("Une autre legende", "https://www.tiktok.com/@ma_chaine/video/7299999999999999999")]


def _lookups(page):
    return [c for c in page.calls if c == ("wait", _sel()["stats"]["post_link"])]


def _lookup_polls(page, ms):
    return [c for c in page.calls if c == ("poll", ms)]


def test_the_post_link_is_read_again_until_the_new_post_appears(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch, page_kwargs={"content_links": OTHER_LINKS})
    page = env.page
    # 1er et 2e relevé : le post programmé n'est pas encore listé ; il apparaît avant le 3e
    page.timeline = [lambda: None, lambda: setattr(page, "content_links", [("Ma legende #un #deux", LINK)])]

    with caplog.at_level("INFO"):
        result = env.publish(mode="scheduled", schedule_at=datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc))

    assert result["post_id"] == "7300000000000000001" and result["post_url"] == LINK
    assert len(_lookups(page)) == 3  # trois essais
    assert len(_lookup_polls(page, 10000)) == 2  # [tiktok] post_lookup_interval_s = 10 entre deux essais
    assert "3 essai(s)" in caplog.text and "20 s" in caplog.text


def test_a_post_that_never_appears_is_left_unfound_after_the_lookup_timeout(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch, page_kwargs={"content_links": OTHER_LINKS})

    with caplog.at_level("INFO"):
        result = env.publish(mode="scheduled", schedule_at=datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc))

    assert result["post_id"] is None and result["post_url"] is None  # jamais d'id inventé (ADR-ad2e)
    assert result["state"] == "scheduled_on_tiktok"
    assert len(_lookups(env.page)) == 7  # 1 essai + un par intervalle de 10 s dans 60 s
    assert len(_lookup_polls(env.page, 10000)) == 6
    assert "7 essai(s)" in caplog.text and "60 s" in caplog.text


def test_the_lookup_interval_and_timeout_are_settings(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"content_links": OTHER_LINKS},
              settings={"post_lookup_interval_s": 5, "post_lookup_timeout_s": 10})

    env.publish()

    assert len(_lookups(env.page)) == 3 and len(_lookup_polls(env.page, 5000)) == 2


def test_a_zero_lookup_timeout_reads_the_page_once(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"content_links": OTHER_LINKS}, settings={"post_lookup_timeout_s": 0})

    result = env.publish()

    assert result["post_id"] is None and len(_lookups(env.page)) == 1


def test_lookup_settings_have_defaults_and_invalid_values_are_refused(tmp_path):
    assert tiktok.CONFIG_DEFAULTS["post_lookup_interval_s"] == 10
    assert tiktok.CONFIG_DEFAULTS["post_lookup_timeout_s"] == 60
    for bad in ({"post_lookup_interval_s": 0}, {"post_lookup_interval_s": "x"}, {"post_lookup_timeout_s": -1},
                {"post_lookup_timeout_s": True}):
        config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "o",
                        _sections={"tiktok": bad})
        with pytest.raises(tiktok.TikTokError, match=r"\[tiktok\] post_lookup_"):
            tiktok.get_settings(config)


def test_clip_payload_reads_mp4_caption_and_hashtags_from_the_sidecar(tmp_path):
    clip = tiktok.clip_payload({"video_id": "aaaaaaaaaaa", "clip_id": "01", "caption": "c", "hashtags": ["#a"]}, tmp_path)
    assert clip == {"video_path": tmp_path / "aaaaaaaaaaa" / "01.mp4", "caption": "c", "hashtags": ["#a"]}
    with pytest.raises(tiktok.TikTokError, match="caption"):
        tiktok.clip_payload({"video_id": "a", "clip_id": "1", "hashtags": []}, tmp_path)


# ---------------------------------------------------------------- (4) R4 arret sur


@pytest.mark.parametrize("case, kwargs, code, words", [
    ("captcha", {"detect": ["captcha"]}, "captcha", "captcha"),
    ("verification", {"detect": ["verification"]}, "verification", "vérification"),
    ("login_marker", {"page_kwargs": {"redirect": "https://www.tiktok.com/login?redirect=x"}}, "login", "connexion expirée"),
    ("login_form", {"detect": ["login"]}, "login", "connexion expirée"),
    ("missing_element", {"remove": ["caption_editor"]}, "element_missing", "caption_editor"),
    ("content_check_failed", {"page_kwargs": {"check": "problem"}}, "content_check_refused", "problème"),
    ("unexpected_page", {"page_kwargs": {"redirect": "https://www.tiktok.com/error"}}, "unexpected_page", "page inattendue"),
])
def test_r4_stops_immediately_with_a_screenshot_and_never_acts_blindly(tmp_path, monkeypatch, case, kwargs, code, words):
    env = Env(tmp_path, monkeypatch, **kwargs)

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == code
    assert words in str(stop.value)
    capture = stop.value.capture
    assert capture is not None and capture.is_file()
    assert capture.parent == Path("state/browser/ma_chaine/captures")
    sel = _sel()
    assert sel["selectors"]["post_button"] not in env.page.clicks()  # jamais de publication apres l'arret
    for solved in sel["detect"]["captcha"] + sel["detect"]["verification"]:
        assert solved not in env.page.clicks()  # un captcha n'est jamais clique ni resolu


def test_r4_captcha_appearing_mid_flow_stops_before_the_post_click(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    sel = _sel()
    env.page.after_click[sel["selectors"]["visibility_dropdown"]] = [sel["detect"]["captcha"][0]]

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "captcha"
    assert sel["selectors"]["post_button"] not in env.page.clicks()


def test_r4_an_unexpected_page_error_is_a_stop_not_a_crash(env):
    env.page.fail_click = True
    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()
    assert stop.value.code == "unexpected_page"
    assert "closed" in str(stop.value)


def test_r4_a_failed_screenshot_is_stated_in_the_reason(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, detect=["captcha"], page_kwargs={"screenshot_error": "disque plein"})
    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()
    assert stop.value.capture is None
    assert "capture d'écran impossible" in str(stop.value) and "disque plein" in str(stop.value)


def test_only_the_caption_and_the_schedule_pickers_are_ever_typed_never_credentials(tmp_path, monkeypatch):
    for kwargs in ({"detect": ["login"]}, {"detect": ["captcha"]}, {}):
        env = Env(tmp_path, monkeypatch, **kwargs)
        try:
            env.publish("scheduled", NOW + timedelta(days=2))
        except tiktok.TikTokStop:
            pass
        assert env.page.fills() == []
        assert "".join(env.page.typed()) in ("", "Ma legende #un #deux")  # la legende, rien d'autre


# ---------------------------------------------------------------- verification de contenu (criteres 4 et 7)


def test_content_check_in_progress_then_ok_waits_before_the_final_click(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "running"})
    sel = _sel()["selectors"]

    def finished():
        env.page.set_check("ok")

    env.page.timeline = [lambda: None, lambda: None, finished]

    result = env.publish()

    assert env.page.waits == 3
    assert env.page.posted == ["now"] and result["state"] == "published"
    polls = [i for i, c in enumerate(env.page.calls) if c[0] == "poll"]
    post = env.page.calls.index(("click", sel["post_button"]))
    assert polls and max(polls) < post  # attente terminee AVANT le clic final
    assert env.ticks >= env.page.waits  # le battement du worker continue pendant l'attente


def test_content_check_ok_text_elsewhere_while_still_running_keeps_waiting(tmp_path, monkeypatch):
    # Relevé réel 2026-10-05 : « Vérification en cours » visible, mais un texte « Aucun problème constaté »
    # existe ailleurs dans la page -> fausse fin, nouvel essai, fenêtre « Continuer à publier ? » revenue.
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "running"})
    sel = _sel()["selectors"]
    env.page.present.add(sel["content_check_ok"])

    def finished():
        env.page.set_check("ok")

    env.page.timeline = [lambda: None, lambda: None, finished]

    result = env.publish()

    assert env.page.waits == 3
    assert env.page.posted == ["now"] and result["state"] == "published"


def test_a_hidden_running_text_left_in_the_page_does_not_block_a_finished_check(tmp_path, monkeypatch):
    # Relevé réel 2026-10-05 : la page affiche « Aucun problème constaté » mais un texte « Vérification en
    # cours » reste dans le DOM sans être affiché : Clipper attendait jusqu'au délai.
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "ok"})
    sel = _sel()["selectors"]
    env.page.present.add(sel["content_check_running"])
    env.page.hidden.add(sel["content_check_running"])

    result = env.publish()

    assert env.page.posted == ["now"] and result["state"] == "published"


def test_content_check_problem_is_an_explicit_r4_failure_and_nothing_is_posted(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "running"})
    env.page.timeline = [lambda: env.page.set_check("problem")]

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "content_check_refused" and "problème" in str(stop.value)  # refus, pas un arrêt R4
    assert stop.value.capture is not None and stop.value.capture.is_file()
    assert env.page.posted == [] and _sel()["selectors"]["post_button"] not in env.page.clicks()


def test_content_check_timeout_is_an_explicit_r4_failure(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "running"},
              settings={"content_check_timeout_s": 10, "poll_interval_s": 5})

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "content_check"
    assert "10 s" in str(stop.value) and "content_check_timeout_s" in str(stop.value)
    assert env.page.waits == 2 and env.page.posted == []


def test_content_check_daily_limit_stops_at_once_without_waiting_for_the_timeout(tmp_path, monkeypatch):
    # Relevé réel 2026-10-20 : « Tu as atteint la limite de vérifications pour aujourd'hui. Réessaie demain. »
    # en mode wait, deux fois 900 s d'attente pour rien avant l'arrêt du compte.
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "running"})
    env.page.present.add(_sel()["selectors"]["content_check_limit"])

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "content_check_limit" and "demain" in str(stop.value)
    assert env.page.waits == 0 and env.page.posted == []


def test_content_check_timeout_defaults_to_900_seconds_and_is_validated(tmp_path):
    assert tiktok.CONFIG_DEFAULTS["content_check_timeout_s"] == 900
    for bad in (0, -1, "900", True):
        config = Config(mode="review", workspace_dir=tmp_path, output_dir=tmp_path,
                        _sections={"tiktok": {"content_check_timeout_s": bad}})
        with pytest.raises(tiktok.TikTokError, match="content_check_timeout_s"):
            tiktok.get_settings(config)


def test_content_check_is_also_awaited_for_a_scheduled_post(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "running"})
    env.page.timeline = [lambda: env.page.set_check("ok")]
    env.publish("scheduled", NOW + timedelta(days=2))
    assert env.page.waits == 1 and env.page.posted == ["scheduled"]


# ---------------------------------------------------------------- interrupteur en mode wait (TASK-0c44)
# Constat réel 08/10 : interrupteur éteint, aucun scan lancé, 900 s d'attente pour rien. En mode wait, le parcours
# l'allume lui-même une fois pour cette vidéo, puis attend le résultat comme avant.


def _switch_selector() -> str:
    return _sel()["selectors"]["content_check_switch"]


def test_wait_turns_the_switch_on_once_then_waits_for_the_result_and_publishes(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "running"})
    env.page.switch["checked"] = False
    env.page.timeline = [lambda: env.page.set_check("ok")]

    with caplog.at_level("INFO"):
        result = env.publish()

    checks = [i for i, c in enumerate(env.page.calls) if c == ("check", _switch_selector())]
    assert len(checks) == 1 and env.page.switch["checked"] is True
    assert env.page.waits == 1  # attente du résultat, après l'allumage
    assert checks[0] < env.page.calls.index(("click", _sel()["selectors"]["post_button"]))
    assert env.page.posted == ["now"] and result["state"] == "published"
    assert "interrupteur allumé pour cette vidéo : [tiktok] content_check = wait" in caplog.text


def test_wait_with_a_greyed_off_switch_is_an_immediate_r4_stop_not_a_900_second_wait(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "ok"})
    env.page.switch.update(checked=False, disabled=True)

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "unexpected_page" and "grisé" in str(stop.value)
    assert env.page.waits == 0 and env.page.posted == [] and ("check", _switch_selector()) not in env.page.calls


def test_wait_without_the_switch_on_the_page_is_an_immediate_element_missing_stop(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "ok"}, remove=("content_check_switch",))

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "element_missing" and "introuvable" in str(stop.value)
    assert env.page.waits == 0 and env.page.posted == []


def test_wait_with_the_switch_already_on_never_clicks_it(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "ok"})

    result = env.publish()

    assert not [c for c in env.page.calls if c[0] in ("check", "uncheck") and c[1] == _switch_selector()]
    assert env.page.posted == ["now"] and result["state"] == "published"


def test_wait_when_tiktok_refuses_the_switch_leaves_it_off_and_is_an_r4_stop(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "ok"})
    env.page.switch["checked"] = False
    env.page.refuse_switch = True

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "unexpected_page" and "allumé" in str(stop.value)
    assert env.page.waits == 0 and env.page.posted == []


# ---------------------------------------------------------------- fenetres surgissantes (criteres 5 et 7)


def test_known_popups_are_closed_with_their_button_and_logged(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch)
    env.page.modals = [
        FakeModal(env.page, "Activer les vérifications automatiques du contenu ?\nAnnuler Activer", ["Annuler", "Activer"]),
        FakeModal(env.page, "Nouvelles fonctionnalités d'édition ajoutées\nJ'ai compris", ["J'ai compris"]),
    ]

    with caplog.at_level("INFO"):
        result = env.publish()

    assert env.page.popups_closed == ["J'ai compris", "Annuler"]  # la dernière dans le DOM = au-dessus, fermée d'abord ; jamais « Activer »
    assert env.page.modals == []
    assert "Activer les vérifications automatiques" in caplog.text and "Annuler" in caplog.text
    assert "Nouvelles fonctionnalités" in caplog.text and "J'ai compris" in caplog.text
    assert result["state"] == "published"


def test_a_known_popup_appearing_mid_flow_is_closed_before_the_next_action(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    sel = _sel()["selectors"]
    modal = FakeModal(env.page, "Nouvelles fonctionnalités d'édition ajoutées", ["J'ai compris"])
    env.page.on_click[sel["schedule_now"]] = lambda: env.page.modals.append(modal)

    env.publish()

    assert env.page.popups_closed == ["J'ai compris"] and env.page.posted == ["now"]


def test_an_unknown_modal_window_is_an_r4_stop_and_is_never_clicked(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.page.modals = [FakeModal(env.page, "Votre compte a été restreint\nOK", ["OK"])]

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "unexpected_page"
    assert "fenêtre" in str(stop.value) and "Votre compte a été restreint" in str(stop.value)
    assert stop.value.capture is not None and stop.value.capture.is_file()
    assert env.page.popups_closed == [] and env.page.posted == []  # jamais de clic de repli


def test_a_known_popup_without_its_button_is_an_r4_stop(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.page.modals = [FakeModal(env.page, "Activer les vérifications automatiques du contenu ?", ["Activer"])]

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "element_missing" and "Annuler" in str(stop.value)
    assert env.page.popups_closed == []  # « Activer » n'est pas le bouton prevu : pas cliqué


def test_a_popup_that_keeps_coming_back_is_an_r4_stop(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)

    class Stubborn(FakeModal):
        def close(self, label):
            self.page.popups_closed.append(label)  # reste affichee

    env.page.modals = [Stubborn(env.page, "Nouvelles fonctionnalités d'édition ajoutées", ["J'ai compris"])]
    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()
    assert stop.value.code == "unexpected_page" and "fenêtre" in str(stop.value)


def test_overlapping_known_windows_close_the_top_one_first_then_the_one_below(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.page.modals_block_clicks = True    # l'overlay du dessus intercepte tout clic sur la bulle du dessous
    below = FakeModal(env.page, "Nouvelles fonctionnalités d'édition ajoutées\nJ'ai compris", ["J'ai compris"])
    top = FakeModal(env.page, "Activer les vérifications automatiques du contenu ?\nAnnuler Activer", ["Annuler", "Activer"])
    env.page.modals = [below, top]         # ordre du DOM : le portail le plus récent (le dernier) est au-dessus

    result = env.publish()

    assert env.page.popups_closed == ["Annuler", "J'ai compris"]  # au-dessus d'abord, jamais « Activer »
    assert env.page.modals == [] and result["state"] == "published"


def test_r4_stop_with_a_capture_also_saves_the_page_html_next_to_the_screenshot(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.page.html = "<html><body>Votre compte a été restreint</body></html>"
    env.page.modals = [FakeModal(env.page, "Votre compte a été restreint\nOK", ["OK"])]

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    html = stop.value.capture.with_suffix(".html")
    assert html.read_text(encoding="utf-8") == "<html><body>Votre compte a été restreint</body></html>"


def test_a_failed_html_save_is_logged_and_never_hides_the_original_r4_stop(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch)
    env.page.content_error = "page fermée"
    env.page.modals = [FakeModal(env.page, "Votre compte a été restreint\nOK", ["OK"])]

    with caplog.at_level("WARNING"), pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "unexpected_page" and "Votre compte a été restreint" in str(stop.value)
    assert stop.value.capture is not None and stop.value.capture.is_file()
    assert not stop.value.capture.with_suffix(".html").exists()
    assert "HTML" in caplog.text and "page fermée" in caplog.text


def _modal_between_guard_and_click(env, selector, modal, times=1):
    """La fenetre surgit APRES la garde de ``wait`` et avant le clic (constat reel du 08/10) : elle s'ajoute
    quand la page rend l'element cherche, ``times`` fois au plus."""
    real, left = env.page.wait_for_selector, [times]

    def wait_for_selector(sel, timeout=None, state=None):
        element = real(sel, timeout=timeout, state=state)
        if sel == selector and left[0] > 0:
            left[0] -= 1
            env.page.modals.append(modal())
        return element

    env.page.wait_for_selector = wait_for_selector
    env.page.modals_block_clicks = True


def _auto_checks_modal(env):
    return lambda: FakeModal(env.page, "Activer les vérifications automatiques du contenu ?\nAnnuler Activer",
                             ["Annuler", "Activer"])


def test_a_known_popup_between_the_guard_and_the_click_is_closed_and_the_click_redone_once(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch)
    post = _sel()["selectors"]["post_button"]
    _modal_between_guard_and_click(env, post, _auto_checks_modal(env))

    with caplog.at_level("INFO"):
        result = env.publish()

    assert env.page.popups_closed == ["Annuler"]            # jamais « Activer »
    assert env.page.clicks().count(post) == 2                # un echec intercepte, puis le clic refait
    assert env.page.posted == ["now"] and result["state"] == "published"
    assert "fenêtre connue" in caplog.text


def test_a_schedule_click_hit_by_a_known_popup_is_redone_after_closing_it(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    later = _sel()["selectors"]["schedule_later"]
    _modal_between_guard_and_click(env, later, _auto_checks_modal(env))

    env.publish(mode="scheduled", schedule_at=NOW + timedelta(days=1))

    assert env.page.popups_closed == ["Annuler"] and env.page.posted == ["scheduled"]
    assert env.page.clicks().count(later) == 2


def test_an_unknown_window_between_the_guard_and_the_click_is_an_r4_stop_never_clicked(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    post = _sel()["selectors"]["post_button"]
    _modal_between_guard_and_click(env, post, lambda: FakeModal(env.page, "Votre compte a été restreint\nOK", ["OK"]))

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "unexpected_page" and "Votre compte a été restreint" in str(stop.value)
    assert env.page.popups_closed == [] and env.page.posted == []
    assert env.page.clicks().count(post) == 1                # pas de second clic


def test_a_click_failing_again_after_the_popup_is_closed_is_an_r4_stop(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    post = _sel()["selectors"]["post_button"]
    _modal_between_guard_and_click(env, post, _auto_checks_modal(env), times=2)   # elle revient apres le re-essai

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "unexpected_page"
    assert env.page.clicks().count(post) == 2                # UN seul nouvel essai
    assert env.page.posted == []


def test_a_click_without_any_window_is_a_single_click_with_the_click_timeout(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    post = _sel()["selectors"]["post_button"]

    env.publish()

    assert env.page.clicks().count(post) == 1 and env.page.posted == ["now"]
    assert set(env.page.click_timeouts) == {10_000}          # click_timeout_s = 10 par defaut, pas les 30 s de Playwright


def test_the_click_timeout_is_a_setting_with_a_default_and_is_validated(tmp_path, monkeypatch):
    assert tiktok.CONFIG_DEFAULTS["click_timeout_s"] == 10
    env = Env(tmp_path, monkeypatch, settings={"click_timeout_s": 3})
    env.publish()
    assert set(env.page.click_timeouts) == {3_000}
    for bad in (0, "10", True):
        config = Config(mode="review", workspace_dir=tmp_path, output_dir=tmp_path, _sections={"tiktok": {"click_timeout_s": bad}})
        with pytest.raises(tiktok.TikTokError, match="click_timeout_s"):
            tiktok.get_settings(config)


def test_a_failing_click_without_interception_nor_window_is_not_retried(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.page.fail_click = True

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "unexpected_page" and "closed" in str(stop.value)
    assert len(env.page.clicks()) == 1


def test_a_missing_chrome_is_a_browser_error_not_a_stop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"x")

    class NoChrome:
        def start(self):
            return self

        chromium = type("C", (), {"launch_persistent_context": staticmethod(
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Chromium distribution 'chrome' is not found")))})()

        def stop(self):
            pass

    class Manager:
        def start(self):
            return NoChrome()

    browser.use_playwright(lambda: Manager())
    try:
        with pytest.raises(browser.BrowserError, match="Chrome"):
            tiktok.publish({"video_path": mp4, "caption": "c", "hashtags": []}, "ma_chaine",
                           mode="immediate", sleep=lambda s: None, now=NOW)
    finally:
        browser.use_playwright(None)


# ---------------------------------------------------------------- (6) delais


def test_action_delays_are_random_bounded_and_beat_the_heartbeat(env):
    env.publish()
    assert len(env.sleeps) >= 4
    assert all(0.3 <= s <= 1 for s in env.sleeps)  # bornes par defaut (rapides, choix utilisateur)
    assert len(set(env.sleeps)) > 1  # pas un intervalle mecanique
    assert env.ticks >= len(env.sleeps)


def test_action_delays_follow_the_configured_bounds(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, settings={"min_action_delay_s": 2, "max_action_delay_s": 8})
    env.publish()
    assert all(2 <= s <= 8 for s in env.sleeps)


@pytest.mark.parametrize("settings", [
    {"min_action_delay_s": 9, "max_action_delay_s": 3},
    {"min_action_delay_s": -1},
    {"max_posts_per_day": 0},
    {"min_gap_minutes": -5},
])
def test_invalid_pacing_settings_are_refused_in_french(tmp_path, monkeypatch, settings):
    env = Env(tmp_path, monkeypatch, settings=settings)
    with pytest.raises(tiktok.TikTokError, match=r"\[tiktok\]"):
        env.publish()
    assert env.opened == []


def test_check_limits_caps_posts_per_day_and_enforces_the_min_gap():
    s = dict(tiktok.CONFIG_DEFAULTS)
    tz = timezone.utc
    day = datetime(2026, 10, 1, 8, 0, tzinfo=tz)
    assert tiktok.check_limits([], day, s, tz) is None
    assert "1 publication" in tiktok.check_limits([day], day.replace(hour=20), s, tz)  # plafond du jour
    assert tiktok.check_limits([day], day + timedelta(days=1), s, tz) is None  # autre jour
    loose = {**s, "max_posts_per_day": 3}
    assert "480" in tiktok.check_limits([day], day + timedelta(hours=7, minutes=59), loose, tz)  # ecart trop court
    assert tiktok.check_limits([day], day + timedelta(hours=8), loose, tz) is None
    assert "480" in tiktok.check_limits([day], day - timedelta(hours=2), loose, tz)  # ecart aussi vers l'avant


# ---------------------------------------------------------------- (5) selecteurs hors du code


def test_selectors_file_says_what_is_verified_for_real_and_what_is_not():
    text = SELECTORS.read_text(encoding="utf-8")
    header = text.split("version =", 1)[0]
    assert "VERIFIE EN REEL" in header and "A VERIFIER SUR LA VRAIE PAGE" in header
    assert "confirmation apres publication" in header.lower().replace("é", "e").replace("è", "e")
    data = tomllib.loads(text)
    for key in tiktok.REQUIRED_SELECTORS:
        assert data["selectors"][key]
    assert data["urls"]["upload"].startswith("https://")


def test_selectors_file_carries_the_real_markers_of_tiktok_studio():
    sel = _sel()["selectors"]
    assert "upload_status_container" in sel["upload_done"] and "Importé" in sel["upload_done"]
    assert "caption_container" in sel["caption_editor"] and "contenteditable" in sel["caption_editor"]
    assert "advanced_settings_container" in sel["advanced_settings"]
    assert "schedule_container" in sel["schedule_now"] and "Maintenant" in sel["schedule_now"]
    assert "schedule_container" in sel["schedule_later"] and "Programmer" in sel["schedule_later"]
    assert "schedule_container" in sel["schedule_inputs"] and "TUXTextInputCore-input" in sel["schedule_inputs"]
    assert "month-title" in sel["calendar_month_title"] and "year-title" in sel["calendar_year_title"]
    assert "arrow" in sel["calendar_arrow"] and "day" in sel["calendar_day"] and "valid" in sel["calendar_day"]
    assert "tiktok-timepicker-left" in sel["timepicker_hour"] and "tiktok-timepicker-right" in sel["timepicker_minute"]
    assert sel["post_button"] == "button[data-e2e='post_video_button']"
    assert sel["discard_button"] == "button[data-e2e='discard_post_button']"
    assert "upload_status_success" not in str(_sel()) and "schedule_video_button" not in str(_sel())
    assert "privacy_container" not in str(_sel()) and "schedule_radio" not in str(_sel())
    assert _sel()["labels"] == {"post_now": "Publier", "post_scheduled": "Programmer",
                                "visibility_public": "Tout le monde", "visibility_private": "Toi uniquement",
                                "visibility_friends": "Ami(e)s"}
    assert _sel()["popups"] == {
        "Activer les vérifications automatiques du contenu": "Annuler",
        "Nouvelles fonctionnalités d'édition ajoutées": "J'ai compris",
        "Prévisualise ta vidéo sur ton téléphone": "J'ai compris",
    }
    assert len(_sel()["calendar"]["months"]) == 12


def test_selectors_file_carries_the_publication_proof_post_link_and_analytics_markers():
    data = _sel()
    assert data["expect"]["published_url_prefix"] == "https://www.tiktok.com/tiktokstudio/content"
    assert "Vidéo publiée" in data["selectors"]["published_marker"]
    assert "Vidéo programmée" in data["selectors"]["published_marker"]  # fenetre d'un post programme
    assert data["continue_publish"] == {"dialog": "Continuer à publier", "cancel": "Annuler"}
    assert data["stats"]["post_link"] == "a[href*='/video/']"
    assert data["urls"]["analytics"] == "https://www.tiktok.com/tiktokstudio/analytics/{post_id}?qa_enter_from=analytics"
    assert data["expect"]["analytics_url_prefix"] == "https://www.tiktok.com/tiktokstudio/analytics/"
    assert data["stats"]["metric_card"] == "[data-tt='VideoOverviewPage_VideoMetricsCard_FlexItem']"  # data-tt, pas css-xxxx
    assert data["metrics"] == {
        "views": "Vues de vidéo", "watch_total": "Temps de lecture total", "watch_avg": "Temps de visionnage moyen",
        "watched_full": "A regardé toute la vidéo", "new_followers": "Nouveaux followers", "retention": "Taux de rétention",
    }
    assert data["stats"]["processing"] == "en cours de traitement"
    assert "css-" not in str(data["stats"])


def test_a_missing_metrics_table_or_analytics_url_is_an_explicit_error(tmp_path):
    import re

    text = SELECTORS.read_text(encoding="utf-8")
    broken = re.sub(r"^\[metrics\]\n(?:(?!\[).*\n)*", "", text, flags=re.M)
    assert broken != text
    bad = tmp_path / "s.toml"
    bad.write_text(broken, encoding="utf-8")
    with pytest.raises(tiktok.TikTokError, match="metrics"):
        tiktok.load_selectors(bad)


def test_missing_selector_key_or_file_is_an_explicit_error(tmp_path):
    bad = tmp_path / "s.toml"
    bad.write_text('[urls]\nupload = "https://x"\n[expect]\nupload_url_prefix = "https://x"\nlogin_url_markers = []\n'
                   '[selectors]\nfile_input = "a"\n[detect]\ncaptcha = []\nverification = []\nlogin = []\n', encoding="utf-8")
    with pytest.raises(tiktok.TikTokError, match="upload_done"):
        tiktok.load_selectors(bad)
    with pytest.raises(tiktok.TikTokError, match="introuvable"):
        tiktok.load_selectors(tmp_path / "absent.toml")


@pytest.mark.parametrize("table", ["popups", "modal", "calendar", "labels"])
def test_a_missing_popup_modal_calendar_or_label_table_is_an_explicit_error(tmp_path, table):
    import re

    text = SELECTORS.read_text(encoding="utf-8")
    broken = re.sub(rf"^\[{table}\]\n(?:(?!\[).*\n)*", "", text, flags=re.M)
    assert broken != text
    bad = tmp_path / "s.toml"
    bad.write_text(broken, encoding="utf-8")
    with pytest.raises(tiktok.TikTokError, match=table):
        tiktok.load_selectors(bad)


def test_no_selector_or_url_is_hardcoded_in_tiktok_py():
    data = tomllib.loads(SELECTORS.read_text(encoding="utf-8"))
    values = set()

    def collect(node):
        if isinstance(node, str):
            if node:  # un selecteur pas encore verifie (« ») n'est pas un selecteur code en dur
                values.add(node)
        elif isinstance(node, dict):
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    collect(data)
    strings = [n.value for n in ast.walk(ast.parse((ROOT / "clipper" / "tiktok.py").read_text(encoding="utf-8")))
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not [s for s in strings if s in values]
    assert not [s for s in strings if "http" in s or "tiktok.com" in s or "data-e2e" in s or "input[" in s]


# ---------------------------------------------------------------- evenements console


def test_events_are_appended_and_read_back_in_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tiktok.emit_event({"level": "error", "account": "ma_chaine", "reason": "captcha"}, now=NOW)
    tiktok.emit_event({"level": "info", "account": "ma_chaine", "reason": "ok"}, now=NOW + timedelta(seconds=1))
    events = tiktok.read_events()
    assert [e["reason"] for e in events] == ["captcha", "ok"]
    assert tiktok.read_events(since=NOW.isoformat())[0]["reason"] == "ok"
    assert (tmp_path / "state" / "tiktok" / "events.json").is_file()


def test_events_file_keeps_only_the_last_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for i in range(tiktok.MAX_EVENTS + 5):
        tiktok.emit_event({"level": "info", "reason": str(i)}, now=NOW + timedelta(seconds=i))
    events = tiktok.read_events()
    assert len(events) == tiktok.MAX_EVENTS and events[-1]["reason"] == str(tiktok.MAX_EVENTS + 4)


def test_corrupt_events_file_is_an_explicit_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "state" / "tiktok" / "events.json"
    path.parent.mkdir(parents=True)
    path.write_text("{pas du json", encoding="utf-8")
    with pytest.raises(tiktok.TikTokError, match="événements"):
        tiktok.read_events()


# ---------------------------------------------------------------- statistiques (SPEC-86fe)


class FakeCell:
    def __init__(self, text=None, href=None):
        self.text, self.href = text, href

    def inner_text(self):
        return self.text

    def get_attribute(self, name):
        return self.href if name == "href" else None


class FakeRow:
    def __init__(self, cells):
        self.cells = cells  # selecteur -> FakeCell ; absent = non affiche

    def query_selector(self, selector):
        return self.cells.get(selector)


def _row(post_id, *, caption="Ma légende #gaming", created="2026-09-30 14:05", visibility="Tout le monde",
         views="1 200", likes="85", comments="7", account="ma_chaine"):
    sel = _sel()["stats"]
    cells = {sel["post_link"]: FakeCell(caption, href=f"https://www.tiktok.com/@{account}/video/{post_id}")}
    for key, value in (("views", views), ("likes", likes), ("comments", comments), ("visibility", visibility),
                       ("created", created)):
        if value is not None:
            cells[sel[key]] = FakeCell(value)
    return FakeRow(cells)


def _cards(*, views="1 200", total="1h:02m:03s", avg="12s", full="23%", followers="4",
           retention="en cours de traitement", only=None):
    """Les cartes de metriques d'un post, texte « libelle | valeur » (releve reel, FR)."""
    labels = _sel()["metrics"]
    values = {"views": views, "watch_total": total, "watch_avg": avg, "watched_full": full,
              "new_followers": followers, "retention": retention}
    return [FakeCell(f"{labels[key]} | {value}") for key, value in values.items()
            if value is not None and (only is None or key in only)]


def _tiles(**values):
    """Les tuiles de la page Donnees analytiques : ``cle=(valeur, evolution)`` ou le texte brut ; le defaut est
    « -- 0 (--) » comme sur la vraie page quand rien n'est encore affiche."""
    labels = _sel()["tiles"]
    out = {}
    for key in labels:
        value = values.get(key, ("0", "--"))
        text = value if isinstance(value, str) else f"-- {value[0]} ({value[1]})"
        out[key] = f"{labels[key]} {text}"
    return out


def _section(title, *lines):
    return FakeCell("\n".join([title, *lines]))


def _viewers():
    labels = _sel()["viewers"]
    return [_section(labels["total"], "1 200"),
            _section(labels["types"], "Récurrents", "40%", "Nouveaux", "60%", "Followers", "25%", "Non followers", "75%"),
            _section(labels["age"], "18-24", "50%", "25-34", "30%", "35+", "20%"),
            _section(labels["gender"], "Femme", "35%", "Homme", "65%"),
            _section(labels["locations"], "France", "80%", "Belgique", "20%")]


def _engagement():
    labels = _sel()["engagement"]
    return [_section(labels["shares"], "12"),
            _section(labels["likes_over_time"], "30 sept.", "40", "1 oct.", "45"),
            _section(labels["comment_words"], "génial", "5", "clip", "3")]


def _curve():
    return [FakeCell("0:00 | 100%"), FakeCell("0:05 | 63%"), FakeCell("0:10 | 23%")]


ID_A, ID_B, ID_C = "7300000000000000001", "7300000000000000002", "7300000000000000003"


def analytics_url(post_id, tab=None):
    key = {None: "analytics", "viewers": "analytics_viewers", "engagement": "analytics_engagement"}[tab]
    return _sel()["urls"][key].format(post_id=post_id)


class Post:
    """Ce que TikTok Studio affiche pour un post : une ligne de la page Publications et ses pages d'analyse."""

    def __init__(self, post_id, *, row=None, cards=None, curve=None, sources=None, viewers="ok", engagement="ok",
                 notice=None):
        self.id = post_id
        self.notice = notice  # bandeau « pas eligible au fil Pour toi » affiche en haut de l'analyse, ou None
        self.row = _row(post_id) if row is None else row
        self.cards = _cards() if cards is None else cards
        self.curve = _curve() if curve is None else curve
        self.sources = sources
        self.viewers = _viewers() if viewers == "ok" else viewers  # « unavailable » : « dès 100 vues »
        self.engagement = _engagement() if engagement == "ok" else engagement


class FakeStudio(FakePage):
    """Les pages de statistiques de TikTok Studio : Donnees analytiques du compte (menu des periodes, tuiles),
    Publications (defilement par lots), analyse d'un post et ses onglets Spectateurs / Engagement."""

    def __init__(self, posts, *, tiles=None, batches=None, **kwargs):
        super().__init__(set(), **kwargs)
        self.posts = {p.id: p for p in posts}
        self.tiles = tiles if tiles is not None else {7: _tiles(), 28: _tiles(), 60: _tiles(), 365: _tiles()}
        self.batches = batches or [[p.id for p in posts]]  # fenetre virtualisee : identifiants dans le DOM apres 0, 1, 2... pas de defilement
        self.scrolled, self.period, self.menu = 0, 7, False
        self.view = ("other", None)
        self.timeouts: list[tuple[str, Any]] = []  # (selecteur, delai) de chaque attente

    def goto(self, url, **kwargs):
        super().goto(url)
        urls, now = _sel()["urls"], self.url
        if now.startswith(urls["stats"]):
            self.view = ("content", None)
        elif now.startswith(urls["analytics_account"]) and now[len(urls["analytics_account"]):][:1] in ("", "?"):
            self.view, self.period, self.menu = ("account", None), 7, False
        elif "/analytics/" in now:
            rest = now.split("/analytics/")[1].split("?")[0].split("/")
            self.view = ({"viewers": "viewers", "engagement": "engagement"}.get(rest[1] if len(rest) > 1 else None, "overview"), rest[0])
        else:
            self.view = ("other", None)

    def _elements(self, selector):
        row = _sel()["stats"]["row"]
        if selector.startswith(row + ", "):  # lignes OU etat vide, attendus en course
            return self._elements(row) + self._elements(selector[len(row) + 2:])
        sel, view, post = _sel(), *self.view
        stats, acc = sel["stats"], sel["account"]
        if selector == sel["modal"]["container"]:
            return list(self.modals)
        if view == "account":
            if selector == acc["tile"]:
                return [FakeCell(text) for text in self.tiles[self.period].values()]
            if selector == acc["period_button"]:
                return [FakeElement(self, selector, text=f"{self.period} derniers jours", on_click=lambda: setattr(self, "menu", True))]
            for days in (7, 28, 60, 365):
                if selector == acc["period_option"].format(days=days) and self.menu:
                    return [FakeElement(self, selector, on_click=lambda days=days: self.pick_period(days))]
        if view == "content":
            if selector == stats["row"]:
                return [self.posts[i].row for i in self.batches[self.scrolled]]  # liste virtualisee : seule la fenetre courante est dans le DOM
        if view == "overview" and post in self.posts:
            if selector == stats["metric_card"]:
                return list(self.posts[post].cards)
            if selector == stats["fyf_notice"] and self.posts[post].notice is not None:
                return [FakeCell(self.posts[post].notice)]
            if selector == stats["retention_point"]:
                return list(self.posts[post].curve)
            if selector == stats["traffic_sources"] and self.posts[post].sources is not None:
                return [FakeCell(self.posts[post].sources)]
        if view in ("viewers", "engagement") and post in self.posts:
            data = getattr(self.posts[post], view)
            if selector == stats["viewers_card" if view == "viewers" else "engagement_card"] and data != "unavailable":
                return list(data)
            if selector == stats["page_text"]:
                return [FakeCell("Les données seront disponibles dès 100 vues." if data == "unavailable" else "")]
        return [FakeElement(self, selector)] if selector in self.present else []

    def pick_period(self, days):
        self.period, self.menu = days, False

    def query_selector(self, selector):
        found = self._elements(selector)
        return found[0] if found else None

    def query_selector_all(self, selector):
        return self._elements(selector)

    def wait_for_selector(self, selector, timeout=None, state=None):
        self.calls.append(("wait", selector))
        self.timeouts.append((selector, timeout))
        found = self._elements(selector)
        if not found:
            raise TimeoutError(f"Timeout {timeout}ms exceeded waiting for {selector}")
        return found[0]

    def evaluate(self, script):
        self.calls.append(("evaluate", script))
        self.scrolled = min(self.scrolled + 1, len(self.batches) - 1)
        return self.scrolled == len(self.batches) - 1  # true = bas du conteneur atteint

    def gotos(self):
        return [c[1] for c in self.calls if c[0] == "goto"]


class StatsEnv:
    """Un releve complet contre de fausses pages ; un sidecar de clip publie (``tiktok_post``) est ecrit pour
    chaque identifiant de ``clips``."""

    def __init__(self, tmp_path, monkeypatch, posts, *, clips=(), detect=None, redirect=None, settings=None, **page_kwargs):
        tmp_path.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(tmp_path)
        self.page = FakeStudio(posts, redirect=redirect, **page_kwargs)
        for kind in (detect or ()):
            self.page.present.add(_sel()["detect"][kind][0])
        self.sleeps: list[float] = []
        self.config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "output",
                             _sections={"tiktok": {"stats_audience_min_views": 0, **(settings or {})}})
        self.folder = tmp_path / "state" / "stats" / "tiktok" / "ma_chaine"
        self.tmp = tmp_path
        for number, post_id in enumerate(clips, start=1):
            self.clip("aaaaaaaaaaa", f"{number:02d}", post_id=post_id)

    def clip(self, video_id, clip_id, post_id=None, url=None, account="ma_chaine", state="published"):
        out = self.tmp / "output" / video_id
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{clip_id}.json").write_text(json.dumps({
            "video_id": video_id, "clip_id": clip_id,
            "tiktok_post": {"url": url, "id": post_id, "state": state, "account": account}}), encoding="utf-8")

    @contextmanager
    def opener(self, account, *, headless):
        self.opened = (account, headless)
        yield FakeContext(self.page)

    def fetch(self, now=NOW, **kwargs):
        return tiktok.fetch_stats("ma_chaine", config=self.config, now=now, opener=self.opener,
                                  sleep=self.sleeps.append, rng=random.Random(1), **kwargs)

    def files(self):
        return sorted(p.name for p in self.folder.glob("*.json"))


# -- reglages


def test_stats_settings_have_defaults_and_invalid_values_are_refused(tmp_path):
    d = tiktok.CONFIG_DEFAULTS
    assert d["stats_interval_h"] == 0 and d["stats_dir"] == "state/stats/tiktok"  # SPEC-47e2 R4 : coupe par defaut
    assert d["stats_stale_min"] == 60
    assert (d["stats_detail_days"], d["stats_detail_max"], d["stats_scroll_rounds"]) == (7, 30, 100)
    for key, bad in (("stats_interval_h", -1), ("stats_interval_h", "24"), ("stats_interval_h", True),
                     ("stats_stale_min", -1), ("stats_stale_min", "60"), ("stats_stale_min", True),
                     ("stats_empty_wait_s", -1), ("stats_empty_wait_s", True),
                     ("stats_detail_days", -1), ("stats_detail_max", "50"), ("stats_scroll_rounds", True)):
        config = Config(mode="review", workspace_dir=tmp_path, output_dir=tmp_path, _sections={"tiktok": {key: bad}})
        with pytest.raises(tiktok.TikTokError, match=key):
            tiktok.get_settings(config)
    for key, good in (("stats_interval_h", 0), ("stats_interval_h", 2.5), ("stats_stale_min", 0)):
        config = Config(mode="review", workspace_dir=tmp_path, output_dir=tmp_path, _sections={"tiktok": {key: good}})
        assert tiktok.get_settings(config)[key] == good  # 0 = coupe, ce n'est pas une erreur


# -- (1) le releve : compte (3 periodes), liste des posts, chaque post


def test_a_full_fetch_reads_the_account_page_then_the_posts_list_then_each_post_in_three_tabs(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A), Post(ID_B)])

    snapshot = env.fetch()

    assert env.opened == ("ma_chaine", False)  # navigateur visible
    assert env.page.gotos() == [
        _sel()["urls"]["analytics_account"], _sel()["urls"]["stats"],
        analytics_url(ID_A), analytics_url(ID_A, "viewers"), analytics_url(ID_A, "engagement"),
        analytics_url(ID_B), analytics_url(ID_B, "viewers"), analytics_url(ID_B, "engagement")]
    assert snapshot["account"] == "ma_chaine" and snapshot["fetched_at"] == NOW.isoformat()
    assert snapshot["origin"] == "full" and snapshot["source"] == "tiktok_studio"
    assert [p["post_id"] for p in snapshot["posts"]] == [ID_A, ID_B]
    assert env.page.fills() == []


def test_the_account_tiles_are_read_for_the_four_periods_with_the_evolution_given_by_tiktok(tmp_path, monkeypatch):
    tiles = {7: _tiles(views=("1 200", "+12,5%"), profile_views=("85", "-3%"), likes=("40", "--"), comments=("0", "--"),
                       shares=("--", "--")),
             28: _tiles(views=("4,8 K", "+8%")), 60: _tiles(views=("9 000", "0%")), 365: _tiles(views=("1,2 M", "+2%"))}
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)], tiles=tiles)

    overview = env.fetch()["overview"]

    assert set(overview) == {"7", "28", "60", "365"}
    assert overview["7"]["views"] == {"value": 1200, "change_pct": 12.5}
    assert overview["7"]["profile_views"] == {"value": 85, "change_pct": -3.0}
    assert overview["7"]["likes"] == {"value": 40, "change_pct": None}  # « (--) » : TikTok ne donne pas l'evolution
    assert overview["7"]["comments"] == {"value": 0, "change_pct": None}  # un vrai zero reste un zero
    assert overview["7"]["shares"] == {"value": None, "change_pct": None}  # « -- » : null explicite
    assert overview["28"]["views"] == {"value": 4800, "change_pct": 8.0}
    assert overview["60"]["views"] == {"value": 9000, "change_pct": 0.0}
    assert overview["365"]["views"] == {"value": 1_200_000, "change_pct": 2.0}


def test_the_period_menu_is_only_clicked_when_the_period_is_not_already_shown(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])
    acc = _sel()["account"]

    env.fetch()

    assert env.page.clicks().count(acc["period_button"]) == 3  # 28, 60 et 365 jours ; 7 jours est deja affiche
    assert env.page.clicks().count(acc["period_option"].format(days=28)) == 1
    assert env.page.clicks().count(acc["period_option"].format(days=60)) == 1
    assert env.page.clicks().count(acc["period_option"].format(days=365)) == 1
    assert acc["period_option"].format(days=7) not in env.page.clicks()
    assert set(env.page.clicks()) <= {acc["period_button"], acc["period_option"].format(days=28),
                                      acc["period_option"].format(days=60), acc["period_option"].format(days=365)}  # rien d'autre n'est clique


def test_a_tile_missing_from_the_page_is_an_explicit_null_never_an_invented_zero(tmp_path, monkeypatch):
    tiles = {n: {k: v for k, v in _tiles().items() if k != "shares"} for n in (7, 28, 60, 365)}
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)], tiles=tiles)

    assert env.fetch()["overview"]["7"]["shares"] == {"value": None, "change_pct": None}


def test_an_unreadable_tile_is_an_unexpected_page_stop_not_a_guess(tmp_path, monkeypatch):
    tiles = {7: _tiles(views="beaucoup (+1%)"), 28: _tiles(), 60: _tiles(), 365: _tiles()}
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)], tiles=tiles)

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.fetch()

    assert stop.value.code == "unexpected_page" and "tuile views" in stop.value.reason


def test_a_period_missing_from_the_menu_is_an_element_missing_stop(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])
    env.page.pick_period = lambda days: None
    real = env.page._elements
    env.page._elements = lambda selector: [] if selector == _sel()["account"]["period_option"].format(days=28) else real(selector)

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.fetch()

    assert stop.value.code == "element_missing" and "28 derniers jours" in stop.value.reason


def test_the_posts_list_comes_from_tiktok_and_includes_posts_published_outside_clipper(tmp_path, monkeypatch):
    posts = [Post(ID_A, row=_row(ID_A, caption="Un clip Clipper #a", visibility="Toi uniquement")),
             Post(ID_B, row=_row(ID_B, caption="Publié à la main", visibility="Ami(e)s", created="1 oct. 2026, 09:30"))]
    env = StatsEnv(tmp_path, monkeypatch, posts, clips=[ID_A])

    by_id = {p["post_id"]: p for p in env.fetch()["posts"]}

    assert set(by_id) == {ID_A, ID_B}  # aucun sidecar pour B : il est quand meme releve
    assert by_id[ID_A]["caption"] == "Un clip Clipper #a" and by_id[ID_A]["visibility"] == "private"
    assert by_id[ID_B]["visibility"] == "friends"
    assert by_id[ID_A]["post_url"] == f"https://www.tiktok.com/@ma_chaine/video/{ID_A}"
    assert by_id[ID_A]["posted_at"] == "2026-09-30T14:05:00" and by_id[ID_A]["posted_at_text"] == "2026-09-30 14:05"
    assert by_id[ID_B]["posted_at"] == "2026-10-01T09:30:00"


def test_an_unknown_date_format_keeps_the_text_and_a_null_date(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A, row=_row(ID_A, created="hier"))])

    post = env.fetch()["posts"][0]

    assert post["posted_at"] is None and post["posted_at_text"] == "hier"


@pytest.mark.parametrize("text,expected", [
    ("2026-10-01 14:05", "2026-10-01T14:05:00"), ("2026-10-01", "2026-10-01T00:00:00"),
    ("01/10/2026 14:05", "2026-10-01T14:05:00"), ("1 oct. 2026, 14:05", "2026-10-01T14:05:00"),
    ("15 septembre 2026", "2026-09-15T00:00:00"), ("31/02/2026", None), ("hier", None), (None, None),
])
def test_dates_of_the_posts_list_are_parsed_or_null(text, expected):
    assert tiktok.parse_date(text, _sel()["calendar"]["months"]) == expected


def test_the_list_is_scrolled_until_it_stops_growing(tmp_path, monkeypatch):
    posts = [Post(ID_A), Post(ID_B), Post(ID_C)]
    env = StatsEnv(tmp_path, monkeypatch, posts, batches=[[ID_A], [ID_A, ID_B], [ID_A, ID_B, ID_C]])

    snapshot = env.fetch()

    assert [p["post_id"] for p in snapshot["posts"]] == [ID_A, ID_B, ID_C]
    scrolls = [c for c in env.page.calls if c[0] == "evaluate"]
    assert scrolls and all(c[1] == _sel()["stats"]["scroll_script"] for c in scrolls)
    assert len(scrolls) == 4  # deux pas qui ajoutent un post (bas atteint), puis deux pas de suite sans nouvelle ligne


def test_a_virtualized_list_of_25_posts_shown_8_at_a_time_is_read_in_full(tmp_path, monkeypatch):
    # releve reel 2026-10-05 : le DOM ne garde que ~8 lignes ; chaque pas de defilement en remplace une partie
    ids = [f"73000000000000{n:05d}" for n in range(25)]
    windows = [ids[0:8], ids[4:12], ids[8:16], ids[12:20], ids[16:24], ids[17:25]]
    env = StatsEnv(tmp_path, monkeypatch, [Post(i) for i in ids], batches=windows)

    assert [p["post_id"] for p in env.fetch()["posts"]] == ids


def test_the_list_that_only_grows_at_the_second_step_is_not_cut_short(tmp_path, monkeypatch):
    posts = [Post(ID_A), Post(ID_B)]
    env = StatsEnv(tmp_path, monkeypatch, posts, batches=[[ID_A], [ID_A], [ID_A, ID_B]])  # le 1er pas ne charge rien

    assert [p["post_id"] for p in env.fetch()["posts"]] == [ID_A, ID_B]


def test_the_end_of_the_list_needs_the_bottom_of_the_container_and_two_idle_steps(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)], batches=[[ID_A], [ID_A], [ID_A], [ID_A]])

    env.fetch()

    assert len([c for c in env.page.calls if c[0] == "evaluate"]) == 3  # 2 pas sans nouvelle ligne ne suffisent pas tant que le bas n'est pas atteint (3e pas)


def test_the_scroll_script_scrolls_the_inner_container_not_the_window():
    script = _sel()["stats"]["scroll_script"]
    assert "scrollTop" in script and "overflowY" in script and "window.scrollTo" not in script


def test_a_list_of_three_batches_is_read_in_full(tmp_path, monkeypatch):
    ids = [f"73000000000000{n:05d}" for n in range(55)]
    env = StatsEnv(tmp_path, monkeypatch, [Post(i) for i in ids],
                   batches=[ids[:20], ids[20:40], ids[40:]])  # fenetres de 20 + 20 + 15 posts (liste virtualisee)

    snapshot = env.fetch()

    assert [p["post_id"] for p in snapshot["posts"]] == ids  # 55 posts lus, aucun perdu
    assert len([c for c in env.page.calls if c[0] == "evaluate"]) == 4  # 2 pas qui ajoutent + 2 pas sans nouvelle ligne : fin


def test_reaching_the_scroll_limit_is_a_logged_error_never_a_silently_truncated_list(tmp_path, monkeypatch, caplog):
    posts = [Post(ID_A), Post(ID_B), Post(ID_C)]
    env = StatsEnv(tmp_path, monkeypatch, posts, batches=[[ID_A], [ID_A, ID_B], [ID_A, ID_B, ID_C]],
                   settings={"stats_scroll_rounds": 1})

    with caplog.at_level("ERROR"), pytest.raises(tiktok.TikTokStop) as stop:
        env.fetch()

    assert stop.value.code == "scroll_limit" and "stats_scroll_rounds" in stop.value.reason
    assert "stats_scroll_rounds" in caplog.text  # journalisee
    assert len([c for c in env.page.calls if c[0] == "evaluate"]) == 1
    assert env.files() == [p.name for p in env.folder.glob("*.error.json")]  # aucun releve tronque dans l'historique
    assert tiktok.read_history("ma_chaine", config=env.config) == []
    assert tiktok.read_error("ma_chaine", config=env.config)["code"] == "scroll_limit"


def test_the_scroll_limit_is_not_reached_when_the_list_stops_growing_just_in_time(tmp_path, monkeypatch):
    posts = [Post(ID_A), Post(ID_B)]
    env = StatsEnv(tmp_path, monkeypatch, posts, batches=[[ID_A], [ID_A, ID_B]], settings={"stats_scroll_rounds": 3})

    assert [p["post_id"] for p in env.fetch()["posts"]] == [ID_A, ID_B]


# -- liste vide : compte neuf sans aucun post (SPEC-47e2 R7)


def test_an_account_without_any_post_gives_zero_posts_quickly_and_no_error(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])  # /tiktokstudio/content vide : aucune ligne

    snapshot = env.fetch()

    assert snapshot["posts"] == [] and snapshot["origin"] == "full"
    assert tiktok.read_error("ma_chaine", config=env.config) is None
    row_waits = [c for c in env.page.timeouts if c[0] == _sel()["stats"]["row"]]  # pas de selecteur d'etat vide
    short = float(tiktok.CONFIG_DEFAULTS["stats_empty_wait_s"]) * 1000
    assert row_waits and all(t is not None and t <= short < 30000 for _, t in row_waits)  # jamais les 30 s
    assert [c for c in env.page.calls if c[0] == "evaluate"] == []  # rien a defiler
    assert tiktok.list_videos("ma_chaine", config=env.config) == []


def test_the_empty_state_selector_is_unverified_and_marked_so_and_a_set_one_is_raced_with_the_rows(tmp_path, monkeypatch):
    text = SELECTORS.read_text(encoding="utf-8")
    assert _sel()["stats"]["empty_state"] == ""  # pas de selecteur invente
    assert "empty_state" in text and "À VÉRIFIER EN RÉEL" in text
    empty = "[data-tt='EmptyState']"
    selectors = _sel()
    selectors["stats"] = {**selectors["stats"], "empty_state": empty}
    env = StatsEnv(tmp_path, monkeypatch, [])
    env.page.present.add(empty)

    snapshot = env.fetch(selectors=selectors)

    assert snapshot["posts"] == []
    assert any(empty in c[0] for c in env.page.timeouts)  # attendu en course avec les lignes, pas apres 30 s


def test_the_snapshot_of_an_account_with_posts_still_waits_for_the_rows_then_scrolls(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])
    assert [p["post_id"] for p in env.fetch()["posts"]] == [ID_A]


def test_filled_metrics_are_read_by_label_and_converted(tmp_path, monkeypatch):
    post = Post(ID_A, row=_row(ID_A, likes="85", comments="7"),
                cards=_cards(views="1 200", total="1h:02m:03s", avg="12s", full="23%", followers="4"))
    env = StatsEnv(tmp_path, monkeypatch, [post])

    got = env.fetch()["posts"][0]

    assert got["views"] == 1200 and isinstance(got["views"], int)
    assert got["watch_total_s"] == 3723.0  # 1h 02m 03s
    assert got["avg_watch_s"] == 12.0
    assert got["watched_full"] == pytest.approx(0.23)  # part vue en entier : pourcentage lu
    assert got["new_followers"] == 4
    assert (got["likes"], got["comments"]) == (85, 7)  # depuis la ligne de la page Publications
    assert got["detailed_at"] == NOW.isoformat()


FYF_NOTICE = ("Cette vidéo n'est pas éligible à la recommandation dans le fil d'actualité Pour toi. Si tu n'es pas "
              "d'accord avec cette restriction de contenu, tu peux envoyer une contestation.")


def test_the_for_you_restriction_banner_is_read_by_text_on_the_post_analytics(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A, notice=FYF_NOTICE), Post(ID_B)])

    by_id = {p["post_id"]: p for p in env.fetch()["posts"]}

    assert by_id[ID_A]["fyf_eligible"] is False and by_id[ID_A]["fyf_notice"] == FYF_NOTICE
    assert by_id[ID_B]["fyf_eligible"] is True and by_id[ID_B]["fyf_notice"] is None  # pas de bandeau : eligible


def test_a_scheduled_post_not_yet_online_has_an_unknown_eligibility(tmp_path, monkeypatch):
    future = Post(ID_A, row=_row(ID_A, created="2026-10-02 18:00"), notice=FYF_NOTICE)
    env = StatsEnv(tmp_path, monkeypatch, [future])

    got = env.fetch()["posts"][0]

    assert got.get("fyf_eligible") is None and got.get("fyf_notice") is None  # jamais devine : pas lu en detail


def test_the_restriction_selector_matches_the_french_and_english_texts_without_generated_classes():
    data = _sel()["stats"]
    pattern = data["fyf_notice"]
    assert "pas éligible à la recommandation" in pattern and "not eligible for recommendation" in pattern
    assert "css-" not in pattern  # classes generees : reperage par le texte


def test_the_restriction_selector_is_required(tmp_path):
    text = tiktok.SELECTORS_PATH.read_text(encoding="utf-8")
    broken = tmp_path / "s.toml"
    broken.write_text(chr(10).join(l for l in text.splitlines() if not l.startswith("fyf_notice")), encoding="utf-8")
    with pytest.raises(tiktok.TikTokError, match="fyf_notice"):
        tiktok.load_selectors(broken)


def test_the_restriction_state_reaches_the_video_list_and_sheet(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A, notice=FYF_NOTICE), Post(ID_B)])
    env.fetch()

    videos = {v["post_id"]: v for v in tiktok.list_videos("ma_chaine", config=env.config)}
    detail = tiktok.video_detail("ma_chaine", ID_A, config=env.config)

    assert videos[ID_A]["fyf_eligible"] is False and videos[ID_A]["fyf_notice"] == FYF_NOTICE
    assert videos[ID_B]["fyf_eligible"] is True
    assert detail["fyf_eligible"] is False


def test_metrics_at_zero_are_real_zeros_not_nulls(tmp_path, monkeypatch):
    post = Post(ID_A, row=_row(ID_A, views="0", likes="0", comments="0"),
                cards=_cards(views="0", total="0h:00m:00s", avg="0s", full="0%", followers="0"))
    env = StatsEnv(tmp_path, monkeypatch, [post])

    got = env.fetch()["posts"][0]

    assert got["views"] == 0 and got["watch_total_s"] == 0.0 and got["avg_watch_s"] == 0.0
    assert got["watched_full"] == 0.0 and got["new_followers"] == 0
    assert got["likes"] == 0 and got["comments"] == 0


def test_values_still_processing_are_explicit_nulls(tmp_path, monkeypatch):
    processing = _sel()["stats"]["processing"]
    post = Post(ID_A, row=_row(ID_A, views=processing), cards=_cards(views=processing, retention=processing),
                sources=processing, viewers="unavailable", engagement="unavailable")
    env = StatsEnv(tmp_path, monkeypatch, [post])

    got = env.fetch()["posts"][0]

    for key in ("views", "retention", "traffic_sources", "viewers", "engagement", "shares"):
        assert key in got and got[key] is None
    assert got["watch_total_s"] == 3723.0  # le reste est lu normalement


def test_a_metric_the_page_does_not_show_is_an_explicit_null_never_an_invented_zero(tmp_path, monkeypatch):
    post = Post(ID_A, row=_row(ID_A, views=None, likes=None, comments="—", visibility=None, created=None),
                cards=_cards(only=("views",)), curve=[])
    env = StatsEnv(tmp_path, monkeypatch, [post])

    got = env.fetch()["posts"][0]

    assert got["views"] == 1200  # la carte d'analyse l'affiche
    for key in ("watch_total_s", "avg_watch_s", "watched_full", "new_followers", "retention", "likes", "comments",
                "visibility", "posted_at", "retention_curve"):
        assert key in got and got[key] is None


def test_the_retention_curve_is_read_point_by_point(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])

    assert env.fetch()["posts"][0]["retention_curve"] == [
        {"t_s": 0.0, "share": 1.0}, {"t_s": 5.0, "share": 0.63}, {"t_s": 10.0, "share": 0.23}]


def test_traffic_sources_are_kept_as_displayed_and_null_when_absent(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A, sources="Pour toi 74 % Abonnés 12 %"), Post(ID_B)])

    by_id = {p["post_id"]: p for p in env.fetch()["posts"]}

    assert by_id[ID_A]["traffic_sources"] == "Pour toi 74 % Abonnés 12 %" and by_id[ID_B]["traffic_sources"] is None


def test_the_viewers_tab_gives_types_age_gender_and_locations(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])

    viewers = env.fetch()["posts"][0]["viewers"]

    assert viewers["total"] == 1200
    assert viewers["types"] == [{"label": "Récurrents", "value": 0.4}, {"label": "Nouveaux", "value": 0.6},
                                {"label": "Followers", "value": 0.25}, {"label": "Non followers", "value": 0.75}]
    assert viewers["age"][0] == {"label": "18-24", "value": 0.5} and len(viewers["age"]) == 3
    assert viewers["gender"] == [{"label": "Femme", "value": 0.35}, {"label": "Homme", "value": 0.65}]
    assert viewers["locations"] == [{"label": "France", "value": 0.8}, {"label": "Belgique", "value": 0.2}]


def test_the_engagement_tab_gives_likes_over_time_comment_words_and_shares(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])

    got = env.fetch()["posts"][0]

    assert got["engagement"]["likes_over_time"] == [{"label": "30 sept.", "value": 40}, {"label": "1 oct.", "value": 45}]
    assert got["engagement"]["comment_words"] == [{"label": "génial", "value": 5}, {"label": "clip", "value": 3}]
    assert got["shares"] == 12 and got["engagement"]["shares"] == 12


def test_a_tab_under_100_views_is_a_null_not_a_stop_but_a_tab_without_cards_or_notice_is_a_stop(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A, viewers="unavailable")])
    got = env.fetch()["posts"][0]
    assert got["viewers"] is None and got["engagement"] is not None

    broken = StatsEnv(tmp_path / "x", monkeypatch, [Post(ID_A, viewers=[])])
    with pytest.raises(tiktok.TikTokStop) as stop:
        broken.fetch()
    assert stop.value.code == "element_missing" and "viewers_card" in stop.value.reason


def test_an_unreadable_card_value_is_an_unexpected_page_stop_not_a_guess(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A, cards=_cards(views="beaucoup"), row=_row(ID_A, views="5"))])

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.fetch()

    assert stop.value.code == "unexpected_page" and "views" in stop.value.reason
    assert stop.value.capture is not None and stop.value.capture.is_file()


def test_an_odd_card_is_an_unexpected_page_stop(tmp_path, monkeypatch):
    labels = _sel()["viewers"]
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A, viewers=[_section(labels["age"], "18-24", "50%", "25-34")])])

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.fetch()

    assert stop.value.code == "unexpected_page" and "couples" in stop.value.reason


@pytest.mark.parametrize("text,expected", [
    ("0h:00m:00s", 0.0), ("1h:02m:03s", 3723.0), ("0h:05m:30s", 330.0), ("12s", 12.0), ("0s", 0.0),
    ("12,5 s", 12.5), ("1:05", 65.0), ("1 min 5 s", 65.0), ("—", None),
])
def test_durations_are_converted_to_seconds(text, expected):
    assert tiktok.parse_duration(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("1 200", 1200), ("1 200", 1200), ("1,2 K", 1200), ("1.5M", 1500000), ("2,5 Md", 2500000000), ("987", 987),
    ("--", None), ("1,432", 1432), ("12,345,678", 12345678), ("1,4 k", 1400), ("2,3 M", 2300000), ("1.4K", 1400),
    ("1 432", 1432), ("999", 999),
])
def test_counts_are_parsed_from_the_displayed_text(text, expected):
    assert tiktok.parse_count(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("+12,5%", 12.5), ("-3 %", -3.0), ("−4%", -4.0), ("4%", 4.0), ("0%", 0.0), ("--", None), ("", None),
    ("4,300.0%", 4300.0), ("+4,300.0%", 4300.0), ("-1,234.5%", -1234.5), ("4 300,0 %", 4300.0),
    ("4 300,0 %", 4300.0), ("4 300,0 %", 4300.0), ("−4 300,0 %", -4300.0),
    ("1,5%", 1.5), ("12,5 %", 12.5), ("12,345,678%", 12345678.0), ("1 234 567,5 %", 1234567.5),
])
def test_evolutions_are_parsed_as_signed_percentages(text, expected):
    assert tiktok.parse_change(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("4,300.0%", 43.0), ("4 300,0 %", 43.0), ("4 300,0 %", 43.0), ("1,5%", 0.015), ("12,5 %", 0.125),
    ("23,4 %", 0.234), ("1,234.5%", 12.345), ("--", None),
])
def test_percentages_accept_a_thousands_separator(text, expected):
    assert tiktok.parse_percent(text) == expected


@pytest.mark.parametrize("text", ["1,2,3%", "1.234.567%", "4,30.0%", "beaucoup", "12 %%"])
def test_ambiguous_percentages_are_refused(text):
    with pytest.raises(ValueError):
        tiktok.parse_change(text)
    with pytest.raises(ValueError):
        tiktok.parse_percent(text)


@pytest.mark.parametrize("text", ["M", "12,5", "1,2", "abc"])
def test_ambiguous_counts_stay_refused(text):
    with pytest.raises(ValueError):
        tiktok.parse_count(text)


def test_an_unreadable_evolution_is_an_error():
    with pytest.raises(ValueError):
        tiktok.parse_change("beaucoup")


# -- (2) l'historique : ajout horodate, jamais d'ecrasement


def test_every_fetch_is_appended_to_the_account_history_and_never_overwrites(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A, cards=_cards(views="10"))])
    env.fetch()
    first = {name: (env.folder / name).read_bytes() for name in env.files()}
    env.page.posts[ID_A].cards = _cards(views="20")

    env.fetch(now=NOW + timedelta(days=1))

    assert len(env.files()) == 2 and tmp_path.joinpath("state/stats/tiktok/ma_chaine").is_dir()
    assert all((env.folder / name).read_bytes() == data for name, data in first.items())  # le 1er releve est intact
    history = tiktok.read_history("ma_chaine", config=env.config)
    assert [s["fetched_at"] for s in history] == [NOW.isoformat(), (NOW + timedelta(days=1)).isoformat()]
    assert [s["posts"][0]["views"] for s in history] == [10, 20]
    assert not [p for p in env.folder.iterdir() if p.suffix == ".tmp"]  # aucun .tmp laisse


def test_two_snapshots_with_the_same_timestamp_never_overwrite_each_other(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])
    env.fetch()
    env.fetch()

    assert len(env.files()) == 2
    assert len(tiktok.read_history("ma_chaine", config=env.config)) == 2


def test_history_is_empty_without_a_fetch_and_a_corrupt_file_is_an_explicit_error(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    assert tiktok.read_history("ma_chaine", config=env.config) == []
    env.folder.mkdir(parents=True)
    (env.folder / "20261001T120000000000Z.json").write_text("{pas du json", encoding="utf-8")
    with pytest.raises(tiktok.TikTokError, match="illisible"):
        tiktok.read_history("ma_chaine", config=env.config)


def _full(day, *, views=None, **posts):
    """Un releve complet synthetique du jour ``day`` (1er octobre 2026 = jour 1) : ``views`` = vues de la tuile 7 jours."""
    stamp = datetime(2026, 10, day, 12, 0, tzinfo=timezone.utc).isoformat()
    labels = list(_sel()["tiles"])
    overview = {str(n): {k: {"value": None if views is None else views * (n // 7) + i, "change_pct": 5.0 if k == "views" else None}
                         for i, k in enumerate(labels)} for n in (7, 28, 60)}
    return {"account": "ma_chaine", "fetched_at": stamp, "source": "tiktok_studio", "origin": "full",
            "overview": overview, "posts": [{"post_id": pid, **fields} for pid, fields in posts.items()]}


def _seed_history(env, *snapshots):
    settings = tiktok.get_settings(env.config)
    for snapshot in snapshots:
        tiktok.append_snapshot("ma_chaine", settings, snapshot)


def test_account_curves_are_computed_from_the_history_with_gaps_left_empty(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    _seed_history(env, _full(1, views=100), _full(2, views=110), _full(4, views=140), _full(10, views=200))

    overview = tiktok.account_overview("ma_chaine", 7, config=env.config)

    series = overview["series"]["views"]
    assert series["labels"] == [f"2026-10-{d:02d}" for d in range(4, 11)]  # 7 jours finissant au dernier releve
    assert series["values"] == [140, None, None, None, None, None, 200]  # jour sans releve : vide, jamais invente
    # periode precedente = la meme courbe decalee de 7 jours : le 8/10 vaut le releve du 1/10, le 9/10 celui du 2/10
    assert series["previous"] == [None, None, None, None, 100, 110, None]
    assert overview["period"] == 7 and overview["snapshots"] == 4
    assert overview["last_full_at"] == _full(10)["fetched_at"] and overview["fetched_at"] == _full(10)["fetched_at"]
    assert set(overview["series"]) == set(_sel()["tiles"])


def test_the_curve_of_a_longer_period_uses_the_matching_tile_of_each_snapshot(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    _seed_history(env, _full(1, views=100), _full(3, views=100))

    series = tiktok.account_overview("ma_chaine", 28, config=env.config)["series"]["views"]

    assert len(series["labels"]) == 28 and series["labels"][-1] == "2026-10-03"
    assert [v for v in series["values"] if v is not None] == [400, 400]  # la tuile 28 jours de chaque releve


def test_tile_evolutions_are_computed_from_the_history_next_to_the_one_given_by_tiktok(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    _seed_history(env, _full(1, views=100), _full(8, views=130))

    tiles = tiktok.account_overview("ma_chaine", 7, config=env.config)["tiles"]

    assert tiles["views"]["value"] == 130 and tiles["views"]["change_pct"] == 5.0  # celle de TikTok
    assert tiles["views"]["history_change_pct"] == pytest.approx(30.0)  # (130 - 100) / 100 : releve d'il y a 7 jours
    assert tiles["profile_views"]["history_change_pct"] == pytest.approx((131 - 101) / 101 * 100)


def test_the_history_evolution_is_null_without_a_snapshot_one_period_earlier_or_a_zero_base(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    _seed_history(env, _full(3, views=100), _full(8, views=130))  # rien le 1er : pas de base
    assert tiktok.account_overview("ma_chaine", 7, config=env.config)["tiles"]["views"]["history_change_pct"] is None

    zero = StatsEnv(tmp_path / "z", monkeypatch, [])
    _seed_history(zero, _full(1, views=0), _full(8, views=130))  # base nulle : jamais de division par zero
    tiles = tiktok.account_overview("ma_chaine", 7, config=zero.config)["tiles"]
    assert tiles["views"]["history_change_pct"] is None and tiles["profile_views"]["history_change_pct"] is not None


def test_the_overview_is_empty_without_a_full_snapshot_and_an_unknown_period_is_refused(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    empty = tiktok.account_overview("ma_chaine", 28, config=env.config)
    assert empty["tiles"] is None and empty["series"] is None and empty["fetched_at"] is None and empty["snapshots"] == 0
    with pytest.raises(tiktok.TikTokError, match="période"):
        tiktok.account_overview("ma_chaine", 14, config=env.config)


def test_a_tile_never_shown_by_tiktok_stays_null_in_the_curve(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    _seed_history(env, _full(1))  # toutes les valeurs null

    overview = tiktok.account_overview("ma_chaine", 7, config=env.config)

    assert overview["tiles"]["views"]["value"] is None and set(overview["series"]["views"]["values"]) == {None}


def test_posts_are_merged_oldest_to_newest_an_absent_key_keeps_the_old_value_but_a_null_replaces_it(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    _seed_history(env, _full(1, **{ID_A: {"views": 10, "viewers": {"total": 10}, "detailed_at": "x"}}),
                  {**_full(2, **{ID_A: {"views": 15, "likes": 3}}), "origin": "opportunistic", "overview": None},
                  _full(3, **{ID_B: {"views": None}}))

    merged = tiktok.merged_posts(tiktok.read_history("ma_chaine", config=env.config))

    assert merged[ID_A]["views"] == 15 and merged[ID_A]["likes"] == 3
    assert merged[ID_A]["viewers"] == {"total": 10} and merged[ID_A]["detailed_at"] == "x"  # detail ancien conserve
    assert merged[ID_A]["first_seen"].startswith("2026-10-01") and merged[ID_A]["last_seen"].startswith("2026-10-02")
    assert merged[ID_B]["views"] is None


def test_only_new_recent_or_processing_posts_are_read_in_detail(tmp_path, monkeypatch):
    old = _row(ID_A, created="2026-08-01 10:00")  # ancien et complet : plus relu
    recent = _row(ID_B, created="2026-09-30 10:00")  # moins de 7 jours : relu
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A, row=old), Post(ID_B, row=recent)])
    env.fetch()
    env.page.posts[ID_C] = Post(ID_C)
    env.page.batches = [[ID_A, ID_B, ID_C]]
    env.page.calls.clear()

    snapshot = env.fetch(now=NOW + timedelta(hours=24))

    overviews = [u for u in env.page.gotos() if u in (analytics_url(i) for i in (ID_A, ID_B, ID_C))]
    assert overviews == [analytics_url(ID_B), analytics_url(ID_C)]
    by_id = {p["post_id"]: p for p in snapshot["posts"]}
    assert "detailed_at" not in by_id[ID_A] and by_id[ID_A]["views"] == 1200  # liste seule
    merged = tiktok.merged_posts(tiktok.read_history("ma_chaine", config=env.config))
    assert merged[ID_A]["viewers"]["total"] == 1200  # la vue fusionnee garde le detail du releve precedent


def test_a_post_with_null_views_is_read_again_and_the_detail_count_is_capped(tmp_path, monkeypatch):
    processing = _sel()["stats"]["processing"]
    post = Post(ID_A, row=_row(ID_A, created="2026-08-01 10:00", views=processing), cards=_cards(views=processing))
    env = StatsEnv(tmp_path, monkeypatch, [post])
    env.fetch()
    env.page.calls.clear()
    env.fetch(now=NOW + timedelta(days=1))
    assert analytics_url(ID_A) in env.page.gotos()  # encore « en cours de traitement » : relu

    capped = StatsEnv(tmp_path / "c", monkeypatch, [Post(ID_A), Post(ID_B), Post(ID_C)], settings={"stats_detail_max": 2})
    snapshot = capped.fetch()
    assert [p["post_id"] for p in snapshot["posts"] if "detailed_at" in p] == [ID_A, ID_B]


# -- TASK-2aa5 : plafond du detail, releve complet, tuile « 24 -2 (-7.7%) », periode 365 jours


def _many_posts(count):
    """``count`` posts, du plus recent (premier de la liste) au plus ancien, tous inconnus de l'historique."""
    ids = [str(7300000000000001000 + n) for n in range(count)]
    return [Post(i) for i in ids], ids


def _opened_analyses(env, ids):
    wanted = {analytics_url(i): i for i in ids}
    return [wanted[u] for u in env.page.gotos() if u in wanted]


def test_the_detail_is_capped_to_the_thirty_most_recent_posts_by_default(tmp_path, monkeypatch):
    posts, ids = _many_posts(40)
    env = StatsEnv(tmp_path, monkeypatch, posts)

    snapshot = env.fetch()

    assert tiktok.CONFIG_DEFAULTS["stats_detail_max"] == 30
    assert _opened_analyses(env, ids) == ids[:30]  # 30 pages d'analyse ouvertes, les plus recentes
    by_id = {p["post_id"]: p for p in snapshot["posts"]}
    assert len(by_id) == 40  # la liste est complete
    for post_id in ids[:30]:
        assert by_id[post_id]["detailed_at"] and by_id[post_id]["detail_not_read"] is False
    for post_id in ids[30:]:
        old = by_id[post_id]
        assert old["views"] == 1200 and old["likes"] == 85 and old["comments"] == 7  # chiffres de la liste
        assert old["detail_not_read"] is True  # « detail non releve »
        for field in ("detailed_at", "retention_curve", "fyf_notice", "fyf_eligible", "traffic_sources", "viewers", "engagement"):
            assert field not in old  # jamais invente


def test_a_full_fetch_has_no_cap(tmp_path, monkeypatch):
    posts, ids = _many_posts(40)
    env = StatsEnv(tmp_path, monkeypatch, posts)

    snapshot = env.fetch(full=True)

    assert _opened_analyses(env, ids) == ids
    assert all(p["detailed_at"] and p["detail_not_read"] is False for p in snapshot["posts"])


def test_a_post_read_in_detail_later_loses_the_not_read_mark_in_the_merged_view(tmp_path, monkeypatch):
    posts, ids = _many_posts(32)
    env = StatsEnv(tmp_path, monkeypatch, posts)
    env.fetch()
    merged = tiktok.merged_posts(tiktok.read_history("ma_chaine", config=env.config))
    assert merged[ids[31]]["detail_not_read"] is True

    env.fetch(now=NOW + timedelta(days=1), full=True)

    merged = tiktok.merged_posts(tiktok.read_history("ma_chaine", config=env.config))
    assert merged[ids[31]]["detail_not_read"] is False and merged[ids[31]]["detailed_at"]


@pytest.mark.parametrize("tile,expected", [
    ("24 -2 (-7.7%)", {"value": 24, "change_pct": -7.7}),
    ("1 432 +30 (+2,1%)", {"value": 1432, "change_pct": 2.1}),
    ("24 (-7.7%)", {"value": 24, "change_pct": -7.7}),
    ("-- 24 (--)", {"value": 24, "change_pct": None}),
    ("-- (--)", {"value": None, "change_pct": None}),
])
def test_a_tile_with_the_absolute_change_before_the_percentage_is_read(tmp_path, monkeypatch, tile, expected):
    tiles = {n: _tiles() for n in (7, 28, 60, 365)}
    tiles[7] = _tiles(views=tile)
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)], tiles=tiles)

    assert env.fetch()["overview"]["7"]["views"] == expected


def test_a_tile_value_with_english_thousands_is_read(tmp_path, monkeypatch):
    tiles = {n: _tiles() for n in (7, 28, 60, 365)}
    tiles[7] = _tiles(views=("1,432", "+1%"))
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A, row=_row(ID_A, views="1,432"), cards=_cards(views="1,432"))], tiles=tiles)

    snapshot = env.fetch()

    assert snapshot["overview"]["7"]["views"]["value"] == 1432 and snapshot["posts"][0]["views"] == 1432


def test_a_missing_365_day_period_is_an_explicit_stop_saying_so(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])
    real = env.page._elements
    env.page._elements = lambda selector: [] if selector == _sel()["account"]["period_option"].format(days=365) else real(selector)

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.fetch()

    assert stop.value.code == "element_missing" and "365 derniers jours" in stop.value.reason


def test_the_overview_of_365_days_is_served_from_the_history(tmp_path, monkeypatch):
    tiles = {n: _tiles() for n in (7, 28, 60, 365)}
    tiles[365] = _tiles(views=("52 000", "+4%"))
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)], tiles=tiles)
    env.fetch()

    out = tiktok.account_overview("ma_chaine", 365, config=env.config)

    assert out["period"] == 365 and out["tiles"]["views"]["value"] == 52000 and len(out["series"]["views"]["labels"]) == 365


# -- (3) liste des videos, fiche d'une video, liens (R3, R5)


def test_the_video_list_gives_each_post_with_its_clipper_link_or_outside_clipper(tmp_path, monkeypatch):
    posts = [Post(ID_A, row=_row(ID_A, caption="Clip Clipper")), Post(ID_B, row=_row(ID_B, caption="À la main")),
             Post(ID_C, row=_row(ID_C, caption="Par l'URL"))]
    env = StatsEnv(tmp_path, monkeypatch, posts)
    env.clip("vid1", "07", post_id=ID_A)
    env.clip("vid2", "03", url=f"https://www.tiktok.com/@ma_chaine/video/{ID_C}")
    env.clip("vid3", "01", post_id=ID_B, account="autre")  # un autre compte ne relie rien
    env.fetch()

    videos = {v["post_id"]: v for v in tiktok.list_videos("ma_chaine", config=env.config)}

    assert videos[ID_A]["clip"] == {"video_id": "vid1", "clip_id": "07"} and videos[ID_A]["outside_clipper"] is False
    assert videos[ID_C]["clip"] == {"video_id": "vid2", "clip_id": "03"}
    assert videos[ID_B]["clip"] is None and videos[ID_B]["outside_clipper"] is True
    assert videos[ID_A]["post_url"].endswith(ID_A) and videos[ID_A]["views"] == 1200
    for key in ("caption", "posted_at", "visibility", "likes", "comments", "shares", "avg_watch_s", "watched_full", "processing"):
        assert key in videos[ID_A]
    assert "viewers" not in videos[ID_A]  # la liste reste legere : le detail est dans la fiche


def _seed_videos(env):
    rows = [("a", "Banane", "2026-09-01 10:00", 500, 5, 10.0, 0.5), ("b", "abricot", "2026-09-03 10:00", 100, 50, 30.0, 0.1),
            ("c", "Cerise", "2026-09-02 10:00", None, None, None, None),
            ("d", "Dattes #fruit", "2026-09-04 10:00", 900, 1, 20.0, 0.9)]
    posts = [{"post_id": f"73{n}", "caption": cap, "posted_at": tiktok.parse_date(date, _sel()["calendar"]["months"]),
              "views": views, "likes": likes, "avg_watch_s": avg, "watched_full": full, "visibility": "public",
              "comments": 0, "shares": None}
             for n, cap, date, views, likes, avg, full in rows]
    _seed_history(env, {**_full(5), "posts": posts})


@pytest.mark.parametrize("kwargs,expected", [
    ({}, "dbca"),                                                # defaut : date de publication, la plus recente en premier
    ({"sort": "posted_at", "descending": False}, "acbd"),
    ({"sort": "views"}, "dabc"), ({"sort": "views", "descending": False}, "badc"),  # sans valeur : toujours en dernier
    ({"sort": "likes"}, "badc"), ({"sort": "avg_watch_s"}, "bdac"), ({"sort": "watched_full"}, "dabc"),
    ({"sort": "caption", "descending": False}, "bacd"), ({"sort": "caption"}, "dcab"),  # sans tenir compte de la casse
    ({"query": "FRUIT"}, "d"), ({"query": "ban"}, "a"), ({"query": "zzz"}, ""), ({"query": "  "}, "dbca"),
])
def test_the_video_list_is_sortable_and_searchable(tmp_path, monkeypatch, kwargs, expected):
    env = StatsEnv(tmp_path, monkeypatch, [])
    _seed_videos(env)

    got = tiktok.list_videos("ma_chaine", config=env.config, **kwargs)

    assert "".join(v["post_id"][-1] for v in got) == expected


def test_an_unknown_sort_column_is_an_explicit_error(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    with pytest.raises(tiktok.TikTokError, match="tri"):
        tiktok.list_videos("ma_chaine", sort="couleur", config=env.config)


def test_a_post_with_null_views_is_listed_as_processing(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    _seed_videos(env)
    by_id = {v["post_id"][-1]: v for v in tiktok.list_videos("ma_chaine", config=env.config)}
    assert by_id["c"]["processing"] is True and by_id["a"]["processing"] is False


def test_the_video_sheet_gives_every_figure_the_links_and_the_history_of_the_post(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A, cards=_cards(views="10"))])
    env.clip("vid1", "07", post_id=ID_A)
    env.fetch()
    env.page.posts[ID_A].cards = _cards(views="20")
    env.fetch(now=NOW + timedelta(days=1))

    sheet = tiktok.video_detail("ma_chaine", ID_A, config=env.config)

    assert sheet["post_id"] == ID_A and sheet["views"] == 20 and sheet["post_url"].endswith(ID_A)
    assert sheet["clip"] == {"video_id": "vid1", "clip_id": "07"} and sheet["outside_clipper"] is False
    assert sheet["viewers"]["total"] == 1200 and sheet["engagement"]["shares"] == 12
    assert sheet["retention_curve"][1] == {"t_s": 5.0, "share": 0.63}
    assert [h["views"] for h in sheet["history"]] == [10, 20]
    assert [h["fetched_at"] for h in sheet["history"]] == [NOW.isoformat(), (NOW + timedelta(days=1)).isoformat()]
    with pytest.raises(tiktok.TikTokError, match="introuvable"):
        tiktok.video_detail("ma_chaine", "999", config=env.config)


def test_a_post_published_outside_clipper_has_a_sheet_without_a_clip(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_B)])
    env.fetch()

    sheet = tiktok.video_detail("ma_chaine", ID_B, config=env.config)

    assert sheet["clip"] is None and sheet["outside_clipper"] is True


# -- (4) arret sur R4, compte, echecs


def test_the_fetch_refuses_a_missing_account_and_the_api_backend(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])
    with pytest.raises(tiktok.TikTokError, match="compte"):
        tiktok.fetch_stats("", config=env.config, opener=env.opener)
    api = Config(mode="review", workspace_dir=tmp_path, output_dir=tmp_path, _sections={"tiktok": {"backend": "api"}})
    with pytest.raises(tiktok.TikTokError, match="api"):
        tiktok.fetch_stats("ma_chaine", config=api)


@pytest.mark.parametrize("kwargs,code", [
    ({"detect": ["captcha"]}, "captcha"),
    ({"detect": ["verification"]}, "verification"),
    ({"detect": ["login"]}, "login"),
    ({"redirect": "https://www.tiktok.com/login?redirect=studio"}, "login"),
    ({"redirect": "https://www.tiktok.com/erreur"}, "unexpected_page"),
])
def test_r4_the_fetch_stops_safely_and_records_the_failure_keeping_the_last_report(tmp_path, monkeypatch, kwargs, code):
    first = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])
    first.fetch()
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)], **kwargs)

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.fetch(now=NOW + timedelta(hours=1))

    assert stop.value.code == code
    assert stop.value.capture is not None and stop.value.capture.is_file()
    assert env.page.clicks() == [] and env.page.fills() == []  # aucun clic, aucune saisie
    history = tiktok.read_history("ma_chaine", config=env.config)
    assert [s["fetched_at"] for s in history] == [NOW.isoformat()]  # le dernier releve est intact, rien d'ajoute
    error = tiktok.read_error("ma_chaine", config=env.config)
    assert error["code"] == code and error["reason"] == stop.value.reason
    assert error["at"] == (NOW + timedelta(hours=1)).isoformat() and error["capture"] == str(stop.value.capture)
    assert len(list(env.folder.glob("*.error.json"))) == 1
    event = tiktok.read_events(config=env.config)[-1]
    assert event["level"] == "error" and event["account"] == "ma_chaine" and stop.value.reason in event["reason"]
    assert event["capture"] == str(stop.value.capture)


def test_a_later_successful_fetch_clears_the_error(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)], detect=["captcha"])
    with pytest.raises(tiktok.TikTokStop):
        env.fetch()
    assert tiktok.read_error("ma_chaine", config=env.config) is not None

    env.page.present.clear()
    env.fetch(now=NOW + timedelta(hours=1))

    assert tiktok.read_error("ma_chaine", config=env.config) is None
    assert len(list(env.folder.glob("*.error.json"))) == 1  # l'echec reste dans l'historique


def test_a_stop_in_the_middle_of_the_posts_writes_no_partial_snapshot(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A), Post(ID_B, cards=_cards(views="beaucoup"))])

    with pytest.raises(tiktok.TikTokStop):
        env.fetch()

    assert tiktok.read_history("ma_chaine", config=env.config) == []


def test_a_missing_chrome_during_the_fetch_is_recorded_and_raised(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])

    @contextmanager
    def no_chrome(account, *, headless):
        raise browser.BrowserError("Chrome introuvable")
        yield

    with pytest.raises(browser.BrowserError):
        tiktok.fetch_stats("ma_chaine", config=env.config, now=NOW, opener=no_chrome)

    assert "Chrome introuvable" in tiktok.read_error("ma_chaine", config=env.config)["reason"]


def test_stats_due_follows_the_interval_since_the_last_full_attempt_success_or_failure(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)], settings={"stats_interval_h": 24})
    assert tiktok.stats_due("ma_chaine", config=env.config, now=NOW)  # jamais releve

    env.fetch()
    assert not tiktok.stats_due("ma_chaine", config=env.config, now=NOW + timedelta(hours=23))
    assert tiktok.stats_due("ma_chaine", config=env.config, now=NOW + timedelta(hours=24))

    env.page.present.add(_sel()["detect"]["captcha"][0])
    with pytest.raises(tiktok.TikTokStop):
        env.fetch(now=NOW + timedelta(hours=24))
    # l'echec compte comme une tentative : pas de nouvelle ouverture du navigateur avant l'intervalle
    assert not tiktok.stats_due("ma_chaine", config=env.config, now=NOW + timedelta(hours=30))
    assert tiktok.stats_due("ma_chaine", config=env.config, now=NOW + timedelta(hours=49))


def test_stats_due_ignores_opportunistic_snapshots(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [], settings={"stats_interval_h": 24})
    _seed_history(env, {**_full(1), "origin": "opportunistic", "overview": None})
    assert tiktok.stats_due("ma_chaine", config=env.config, now=datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc))


# -- (4b) releve seulement a l'usage : periment a l'ouverture de l'ecran Statistiques (SPEC-47e2 R4)


def test_stats_are_stale_when_never_fetched_or_older_than_stats_stale_min(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])
    assert tiktok.stats_stale("ma_chaine", config=env.config, now=NOW)  # jamais releve

    env.fetch()
    assert not tiktok.stats_stale("ma_chaine", config=env.config, now=NOW + timedelta(minutes=59))  # frais : rien
    assert tiktok.stats_stale("ma_chaine", config=env.config, now=NOW + timedelta(minutes=60))  # perime : releve


def test_stats_stale_min_zero_means_never_on_opening(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)], settings={"stats_stale_min": 0})
    assert not tiktok.stats_stale("ma_chaine", config=env.config, now=NOW)  # meme jamais releve
    env.fetch()
    assert not tiktok.stats_stale("ma_chaine", config=env.config, now=NOW + timedelta(days=30))


def test_stats_stale_follows_the_configured_minutes_and_ignores_opportunistic_snapshots(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [], settings={"stats_stale_min": 10})
    _seed_history(env, {**_full(1), "origin": "opportunistic", "overview": None})
    assert tiktok.stats_stale("ma_chaine", config=env.config, now=datetime(2026, 10, 1, 12, 5, tzinfo=timezone.utc))

    _seed_history(env, _full(1))
    assert not tiktok.stats_stale("ma_chaine", config=env.config, now=datetime(2026, 10, 1, 12, 9, tzinfo=timezone.utc))
    assert tiktok.stats_stale("ma_chaine", config=env.config, now=datetime(2026, 10, 1, 12, 10, tzinfo=timezone.utc))


def test_a_failed_attempt_is_not_retried_on_every_opening(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])
    env.page.present.add(_sel()["detect"]["captcha"][0])
    with pytest.raises(tiktok.TikTokStop):
        env.fetch()  # arret sur : le navigateur n'est pas rouvert a chaque ouverture de l'ecran
    assert not tiktok.stats_stale("ma_chaine", config=env.config, now=NOW + timedelta(minutes=30))
    assert tiktok.stats_stale("ma_chaine", config=env.config, now=NOW + timedelta(minutes=61))


def test_the_worker_stats_interval_defaults_to_off(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])
    assert not tiktok.stats_due("ma_chaine", config=env.config, now=NOW)  # coupe par defaut, meme jamais releve
    env.fetch()
    assert not tiktok.stats_due("ma_chaine", config=env.config, now=NOW + timedelta(days=30))


def test_the_stats_interval_of_zero_is_off_and_negative_is_an_error(tmp_path, monkeypatch):
    off = StatsEnv(tmp_path / "a", monkeypatch, [], settings={"stats_interval_h": 0})
    assert tiktok.get_settings(off.config)["stats_interval_h"] == 0
    bad = Config(mode="review", workspace_dir=tmp_path, output_dir=tmp_path, _sections={"tiktok": {"stats_interval_h": -2}})
    with pytest.raises(tiktok.TikTokError, match="stats_interval_h"):
        tiktok.stats_due("ma_chaine", config=bad, now=NOW)


# -- (4c) posts supprimes sur TikTok : absents du dernier releve complet (SPEC-47e2 R2 : l'historique reste)


def test_a_post_missing_from_the_latest_full_snapshot_is_no_longer_listed_but_the_history_is_kept(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    _seed_history(env, _full(1, **{ID_A: {"views": 10}, ID_B: {"views": 20}}), _full(2, **{ID_A: {"views": 15}}))

    videos = tiktok.list_videos("ma_chaine", config=env.config)

    assert [v["post_id"] for v in videos] == [ID_A]  # b supprime : n'apparait plus
    history = tiktok.read_history("ma_chaine", config=env.config)
    assert [[p["post_id"] for p in s["posts"]] for s in history] == [[ID_A, ID_B], [ID_A]]  # rien d'efface
    assert tiktok.deleted_post_ids(history) == {ID_B}
    with pytest.raises(tiktok.TikTokError, match="introuvable"):
        tiktok.video_detail("ma_chaine", ID_B, config=env.config)
    assert tiktok.video_detail("ma_chaine", ID_A, config=env.config)["views"] == 15


def test_every_post_is_listed_while_no_full_snapshot_exists_and_an_opportunistic_one_deletes_nothing(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    only_a = {**_full(2, **{ID_A: {"views": 15}}), "origin": "opportunistic", "overview": None}
    _seed_history(env, {**_full(1, **{ID_A: {"views": 10}, ID_B: {"views": 20}}), "origin": "opportunistic", "overview": None}, only_a)
    assert {v["post_id"] for v in tiktok.list_videos("ma_chaine", config=env.config)} == {ID_A, ID_B}  # pas de releve complet

    _seed_history(env, _full(3, **{ID_A: {"views": 1}, ID_B: {"views": 2}}), {**only_a, "fetched_at": _full(4)["fetched_at"]})
    assert {v["post_id"] for v in tiktok.list_videos("ma_chaine", config=env.config)} == {ID_A, ID_B}  # la page vue au passage est partielle


def test_a_deleted_post_that_comes_back_in_a_later_full_snapshot_is_listed_again(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    _seed_history(env, _full(1, **{ID_A: {}, ID_B: {}}), _full(2, **{ID_A: {}}), _full(3, **{ID_A: {}, ID_B: {}}))
    assert {v["post_id"] for v in tiktok.list_videos("ma_chaine", config=env.config)} == {ID_A, ID_B}


def test_two_real_fetches_where_a_post_disappears(tmp_path, monkeypatch):
    first = StatsEnv(tmp_path, monkeypatch, [Post(ID_A), Post(ID_B)])
    first.fetch()
    second = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])
    second.fetch(now=NOW + timedelta(hours=1))

    assert [v["post_id"] for v in tiktok.list_videos("ma_chaine", config=second.config)] == [ID_A]
    assert len(tiktok.read_history("ma_chaine", config=second.config)) == 2


def test_a_post_seen_only_after_the_latest_full_snapshot_is_not_deleted_and_a_real_deletion_still_is(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [])
    opportunistic = {**_full(3, **{ID_A: {"views": 15}, ID_C: {"views": 0}}), "origin": "opportunistic", "overview": None}
    _seed_history(env, _full(1, **{ID_A: {"views": 10}, ID_B: {"views": 20}}), _full(2, **{ID_A: {"views": 15}}), opportunistic)

    history = tiktok.read_history("ma_chaine", config=env.config)
    assert tiktok.deleted_post_ids(history) == {ID_B}  # C vu apres le dernier complet : jamais supprime
    assert {v["post_id"] for v in tiktok.list_videos("ma_chaine", config=env.config)} == {ID_A, ID_C}
    assert tiktok.video_detail("ma_chaine", ID_C, config=env.config)["views"] == 0  # la fiche le rend, pas « supprimee »
    with pytest.raises(tiktok.TikTokError, match="introuvable"):
        tiktok.video_detail("ma_chaine", ID_B, config=env.config)  # vraie suppression toujours detectee


def test_a_scheduled_post_not_yet_online_is_listed_but_never_read_in_detail(tmp_path, monkeypatch):
    scheduled = Post(ID_A, row=_row(ID_A, created="2026-10-02 18:00"))  # programme : apres NOW
    env = StatsEnv(tmp_path, monkeypatch, [scheduled, Post(ID_B)])

    snapshot = env.fetch()

    assert analytics_url(ID_A) not in env.page.gotos()  # aucune page d'analyse ouverte pour lui
    by_id = {p["post_id"]: p for p in snapshot["posts"]}
    assert by_id[ID_A]["detail_not_read"] is True and "detailed_at" not in by_id[ID_A]  # pas de mesure inventee
    assert "avg_watch_s" not in by_id[ID_A] or by_id[ID_A]["avg_watch_s"] is None
    assert by_id[ID_B]["detailed_at"] and by_id[ID_B]["avg_watch_s"] == 12.0  # le post en ligne est lu comme avant


# -- (5) releve opportuniste : la page Publications est deja affichee pour autre chose


def _opportunistic_env(tmp_path, monkeypatch, rows=None):
    env = Env(tmp_path, monkeypatch)
    env.page.lists[_sel()["stats"]["row"]] = [_row(ID_A, caption="Ma legende #un #deux", views="1 200", likes="85",
                                                   comments="7"), _row(ID_B, views="30", likes="2", comments="0")] if rows is None else rows
    return env


def _published_history(env):
    return tiktok.read_history("ma_chaine", config=env.config)


def test_a_finished_publication_reads_the_posts_list_on_the_page_and_appends_it_to_the_history(tmp_path, monkeypatch):
    env = _opportunistic_env(tmp_path, monkeypatch)

    result = env.publish()

    assert result["state"] == "published" and result["post_id"] == ID_A  # la publication n'est pas touchee
    history = _published_history(env)
    assert len(history) == 1 and history[0]["origin"] == "opportunistic" and history[0]["overview"] is None
    assert history[0]["fetched_at"] == NOW.isoformat()
    posts = {p["post_id"]: p for p in history[0]["posts"]}
    assert (posts[ID_A]["views"], posts[ID_A]["likes"], posts[ID_A]["comments"]) == (1200, 85, 7)
    assert (posts[ID_B]["views"], posts[ID_B]["likes"], posts[ID_B]["comments"]) == (30, 2, 0)
    assert "detailed_at" not in posts[ID_A] and "viewers" not in posts[ID_A]  # que ce que la page affiche


def test_the_opportunistic_read_adds_no_navigation_and_no_click(tmp_path, monkeypatch):
    (tmp_path / "plain").mkdir()
    plain = Env(tmp_path / "plain", monkeypatch)
    plain.publish()
    env = _opportunistic_env(tmp_path, monkeypatch)

    env.publish()

    assert [c for c in env.page.calls if c[0] == "goto"] == [("goto", _sel()["urls"]["upload"])]
    same = lambda page: [c for c in page.calls if c[0] != "upload"]  # le chemin du mp4 differe, rien d'autre
    assert same(env.page) == same(plain.page)  # exactement les memes actions que sans releve
    assert env.page.waits == plain.page.waits  # aucune attente en plus


def test_the_opportunistic_read_happens_once_per_browser_session(tmp_path, monkeypatch):
    env = _opportunistic_env(tmp_path, monkeypatch)
    env.publish()
    assert len(_published_history(env)) == 1


def test_a_page_that_cannot_be_read_never_fails_the_publication_and_is_logged(tmp_path, monkeypatch, caplog):
    broken = FakeRow({})  # une ligne sans lien de post
    env = _opportunistic_env(tmp_path, monkeypatch, rows=[broken])

    with caplog.at_level("WARNING"):
        result = env.publish()

    assert result["state"] == "published"
    assert _published_history(env) == []
    assert "relevé au passage impossible" in caplog.text


def test_an_unreadable_value_on_the_page_is_not_a_stop_during_a_publication(tmp_path, monkeypatch):
    env = _opportunistic_env(tmp_path, monkeypatch, rows=[_row(ID_A, views="beaucoup")])

    result = env.publish()

    assert result["state"] == "published" and _published_history(env) == []
    assert not list((tmp_path / "state").rglob("*.png")) and not list(tmp_path.rglob("captures"))  # pas de capture


def test_a_history_write_failure_during_a_publication_never_fails_the_publication(tmp_path, monkeypatch, caplog):
    env = _opportunistic_env(tmp_path, monkeypatch)

    def full_disk(*args, **kwargs):
        raise OSError("disque plein")

    monkeypatch.setattr(tiktok, "append_snapshot", full_disk)

    with caplog.at_level("WARNING"):
        result = env.publish()

    assert result["state"] == "published" and result["post_id"] == ID_A
    assert "disque plein" in caplog.text  # dit dans le journal, jamais avale


def test_no_row_displayed_means_no_snapshot(tmp_path, monkeypatch):
    env = _opportunistic_env(tmp_path, monkeypatch, rows=[])
    env.publish()
    assert _published_history(env) == []


def test_any_visit_of_the_posts_page_with_the_robot_already_there_is_read_in_passing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    page = FakeStudio([Post(ID_A), Post(ID_B)])
    page.goto(_sel()["urls"]["stats"])  # la page Publications est deja affichee (verification de connexion...)
    settings = tiktok.get_settings(None)
    flow = tiktok._Flow(page, "ma_chaine", _sel(), settings, now=NOW, sleep=lambda s: None, rng=random.Random(1),
                        on_tick=None, harvest=True)

    flow.guard()
    flow.guard()

    config = Config(mode="review", workspace_dir=tmp_path, output_dir=tmp_path)
    history = tiktok.read_history("ma_chaine", config=config)
    assert len(history) == 1 and [p["post_id"] for p in history[0]["posts"]] == [ID_A, ID_B]
    assert page.calls == [("goto", _sel()["urls"]["stats"])]  # aucune navigation ajoutee


def test_a_full_fetch_does_not_also_write_an_opportunistic_snapshot(tmp_path, monkeypatch):
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A)])
    env.fetch()
    assert [s["origin"] for s in tiktok.read_history("ma_chaine", config=env.config)] == ["full"]


def test_opportunistic_posts_are_merged_into_the_video_list(tmp_path, monkeypatch):
    env = _opportunistic_env(tmp_path, monkeypatch)
    env.publish()

    videos = {v["post_id"]: v for v in tiktok.list_videos("ma_chaine", config=env.config)}

    assert videos[ID_A]["views"] == 1200 and videos[ID_B]["likes"] == 2
    assert videos[ID_A]["avg_watch_s"] is None  # pas encore releve en detail : null explicite


# -- selecteurs


def test_stats_selectors_live_in_the_selectors_file_marked_to_verify():
    text = SELECTORS.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["urls"]["stats"].startswith("https://") and data["expect"]["stats_url_prefix"].startswith("https://")
    for key in tiktok.REQUIRED_STATS_SELECTORS:
        assert data["stats"][key]
    assert text.count("A VERIFIER SUR LA VRAIE PAGE") >= 2  # un second marquage pour les pages de statistiques


def test_the_account_page_selectors_and_labels_are_in_the_selectors_file():
    data = _sel()
    assert data["urls"]["analytics_account"] == "https://www.tiktok.com/tiktokstudio/analytics"
    assert data["urls"]["analytics_viewers"].endswith("/{post_id}/viewers?qa_enter_from=analytics")
    assert data["urls"]["analytics_engagement"].endswith("/{post_id}/engagement?qa_enter_from=analytics")
    assert data["tiles"] == {"views": "Vues de la vidéo", "profile_views": "Vues du profil", "likes": "J'aime",
                             "comments": "Commentaires", "shares": "Partages"}
    assert "{days}" in data["account"]["period_label"] and "{days}" in data["account"]["period_option"]
    assert data["stats"]["unavailable"] == ["dès 100 vues", "en cours de traitement", "atteindront le nombre de 100",
                                                "Tu pourras voir ces informations"]
    assert data["viewers"]["age"] == "Âge" and data["engagement"]["comment_words"].startswith("Mots les plus utilisés")


@pytest.mark.parametrize("table,key", [("account", "tile"), ("tiles", "shares"), ("viewers", "locations"),
                                       ("engagement", "likes_over_time")])
def test_a_missing_account_or_tab_label_is_an_explicit_error(tmp_path, table, key):
    text = SELECTORS.read_text(encoding="utf-8")
    broken = text.replace(f"\n{key} = ", f"\n{key}_absent = ", 1)
    assert broken != text
    bad = tmp_path / "s.toml"
    bad.write_text(broken, encoding="utf-8")
    with pytest.raises(tiktok.TikTokError, match=rf"\[{table}\] {key}"):
        tiktok.load_selectors(bad)


def test_a_missing_stats_selector_is_an_explicit_error(tmp_path):
    text = SELECTORS.read_text(encoding="utf-8").replace("[stats]", "[stats_absent]")
    bad = tmp_path / "s.toml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(tiktok.TikTokError, match=r"\[stats\] row"):
        tiktok.load_selectors(bad)


class FakeSwitch:
    def __init__(self, checked, disabled=False):
        self.checked, self.unchecks, self.disabled = checked, 0, disabled

    def is_checked(self):
        return self.checked

    def is_disabled(self):
        return self.disabled

    def uncheck(self, **kwargs):
        self.unchecks += 1
        if not self.disabled:  # TikTok grise l'interrupteur : le clic ne change rien
            self.checked = False


def test_content_check_off_with_the_switch_greyed_by_the_daily_limit_publishes_without_unchecking(tmp_path, monkeypatch):
    # Relevé réel 2026-10-04 : « Tu as atteint la limite de vérifications pour aujourd'hui » -> interrupteur
    # coché mais désactivé ; uncheck(force=True) ne change rien et arrêtait la publication (unexpected_page).
    env = Env(tmp_path, monkeypatch, settings={"content_check": "off"})
    switch = FakeSwitch(True, disabled=True)
    real = env.page.query_selector
    env.page.query_selector = lambda css: switch if css == _sel()["selectors"]["content_check_switch"] else real(css)
    env.publish()
    assert switch.unchecks == 0


def test_content_check_off_turns_the_switch_off_and_never_waits(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, settings={"content_check": "off"})
    switch = FakeSwitch(True)
    real = env.page.query_selector
    env.page.query_selector = lambda css: switch if css == _sel()["selectors"]["content_check_switch"] else real(css)
    env.publish()
    assert switch.unchecks == 1 and switch.checked is False


def test_content_check_defaults_to_off():
    assert tiktok.CONFIG_DEFAULTS["content_check"] == "off"


# ---------------------------------------------------------------- reglages par post (SPEC-1ed3 R2)


def test_post_options_drive_visibility_comments_reuse_and_ai_label_without_clicking_what_is_already_set(env):
    # etat initial de TikTok : tout le monde, commentaires et reutilisation cochees, contenu IA coupe
    env.publish(options={"allow_comments": True, "allow_reuse": True, "ai_generated": False})
    assert env.page.toggles == []  # deja dans l'etat voulu : aucun clic


def test_post_options_toggle_only_the_boxes_that_differ(env):
    env.publish(options={"allow_comments": False, "allow_reuse": True, "ai_generated": True})
    assert env.page.toggles == [("comment_switch", False), ("ai_switch", True)]
    assert env.page.options == {"comment_switch": False, "reuse_switch": True, "ai_switch": True}


def test_post_options_reuse_off(env):
    env.publish(options={"allow_reuse": False})
    assert env.page.toggles == [("reuse_switch", False)]


def test_settings_blocks_are_opened_with_show_more_before_the_options_are_read(tmp_path, monkeypatch):
    sel = _sel()["selectors"]
    folded = Env(tmp_path, monkeypatch, page_kwargs={"folded": True})
    folded.publish(options={"ai_generated": True})
    assert sel["advanced_settings"] in folded.page.clicks()
    assert folded.page.toggles == [("ai_switch", True)]


def test_friends_visibility_selects_the_friends_option(env):
    env.publish(options={"visibility": "friends"})
    sel = _sel()["selectors"]
    assert env.page.clicks() == [sel["caption_editor"], sel["visibility_dropdown"], sel["visibility_friends"],
                                 sel["schedule_now"], sel["post_button"]]


def test_post_options_default_to_the_tiktok_section(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, settings={"allow_comments": False, "ai_generated": True})
    env.publish()
    assert env.page.toggles == [("comment_switch", False), ("ai_switch", True)]  # reglage [tiktok], sans options


def test_post_options_override_the_tiktok_section(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, settings={"allow_comments": False})
    env.publish(options={"allow_comments": True})
    assert env.page.toggles == []


def test_post_options_content_check_wait_overrides_off(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, settings={"content_check": "off"})
    switch = FakeSwitch(True)
    real = env.page.query_selector
    env.page.query_selector = lambda css: switch if css == _sel()["selectors"]["content_check_switch"] else real(css)
    env.publish(options={"content_check": "wait"})
    assert switch.unchecks == 0  # « wait » : on attend le resultat, on ne coupe pas l'interrupteur


def test_private_post_option_with_schedule_is_refused_before_any_browser_is_opened(env):
    when = NOW + timedelta(days=2)
    with pytest.raises(tiktok.TikTokError, match="privée"):
        env.publish("scheduled", when, options={"visibility": "private"})
    assert env.opened == []


@pytest.mark.parametrize("options, message", [
    ({"visibility": "secret"}, "visibility"),
    ({"allow_comments": "oui"}, "allow_comments"),
    ({"ai_generated": 1}, "ai_generated"),
    ({"content_check": "never"}, "content_check"),
    ({"inconnu": True}, "inconnu"),
])
def test_invalid_post_options_are_an_explicit_error(env, options, message):
    with pytest.raises(tiktok.TikTokError, match=message):
        env.publish(options=options)
    assert env.opened == []


def test_a_missing_option_block_is_an_r4_stop_with_a_capture_never_a_blind_click(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.page.present.discard(_sel()["selectors"]["ai_switch"])
    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish(options={"ai_generated": True})
    assert stop.value.code == "element_missing" and "contenu généré par IA" in stop.value.reason
    assert env.page.posted == []


def test_option_selectors_target_the_nearest_label_never_a_position():
    sel = _sel()["selectors"]
    assert "user_perm_container" in sel["comment_switch"] and 'text="Commentaire"' in sel["comment_switch"]
    assert "user_perm_container" in sel["reuse_switch"] and 'text="Réutilisation du contenu"' in sel["reuse_switch"]
    assert "aigc_container" in sel["ai_switch"] and "Contenu généré par IA" in sel["ai_switch"]
    assert "role='switch'" in sel["ai_switch"]
    for key in ("comment_switch", "reuse_switch", "ai_switch"):
        assert "ancestor::div" in sel[key] and "nth" not in sel[key]
    assert "option-\"2\"" in sel["visibility_friends"] and "Ami(e)s" in sel["visibility_friends"]
    assert _sel()["labels"]["visibility_friends"] == "Ami(e)s"


def test_new_option_selectors_are_required_in_the_selectors_file():
    for key in ("comment_switch", "reuse_switch", "ai_switch", "visibility_friends"):
        assert key in tiktok.REQUIRED_SELECTORS


def test_tiktok_option_defaults_match_the_tiktok_page_defaults():
    d = tiktok.CONFIG_DEFAULTS
    assert (d["allow_comments"], d["allow_reuse"], d["ai_generated"]) == (True, True, False)


# ---------------------------------------------------------------- prochaine heure possible (SPEC-1ed3 R4)


def test_next_allowed_returns_the_first_time_that_respects_the_caps():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("UTC")
    settings = {**tiktok.CONFIG_DEFAULTS, "max_posts_per_day": 2, "min_gap_minutes": 120}
    times = [NOW.replace(hour=8), NOW.replace(hour=10)]  # deux posts : plafond du jour atteint
    assert tiktok.next_allowed(times, NOW, settings, tz) == datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)
    # un seul post : le prochain creneau est la fin de l'ecart minimal
    assert tiktok.next_allowed([NOW.replace(hour=11)], NOW, settings, tz) == NOW.replace(hour=13)
    # aucun obstacle : l'heure demandee elle-meme
    assert tiktok.next_allowed([], NOW, settings, tz) == NOW


def test_a_setting_disabled_by_tiktok_is_logged_and_skipped_not_a_stop(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch, settings={"allow_reuse": True})
    env.page.options["reuse_switch"] = False
    env.page.disabled_options.add("reuse_switch")
    env.publish()
    assert env.page.options["reuse_switch"] is False  # laissee telle quelle, aucun arret
    assert any("désactivée par TikTok" in r.getMessage() for r in caplog.records)


def test_publications_date_without_year_uses_the_year_of_the_reading():
    months = _sel()["calendar"]["months"]
    assert tiktok.parse_date("2 oct., 12:30", months, datetime(2026, 10, 2, 0, 5)) == "2026-10-02T12:30:00"
    assert tiktok.parse_date("28 déc., 09:00", months, datetime(2027, 1, 3)) == "2026-12-28T09:00:00"
    assert tiktok.parse_date("2 oct., 12:30", months) is None  # sans date de releve : pas d'annee inventee


def test_viewers_and_engagement_are_skipped_below_the_audience_threshold(tmp_path, monkeypatch):
    # Sous 100 vues, TikTok laisse ces onglets vides : les ouvrir coutait deux attentes de 30 s par post.
    env = StatsEnv(tmp_path, monkeypatch, [Post(ID_A, cards=_cards(views="20"))], settings={"stats_audience_min_views": 100})
    env.fetch()
    post = tiktok.video_detail("ma_chaine", ID_A, config=env.config)
    assert post["viewers"] is None and post["engagement"] is None
    assert tiktok.CONFIG_DEFAULTS["stats_audience_min_views"] == 100


# ---------------------------------------------------------------- [browser] pilot_wait_s (TASK-2456, revue M5)


def test_the_pilot_wait_of_the_config_reaches_the_browser_for_publishing_and_stats(env, monkeypatch):
    monkeypatch.setitem(browser.CONFIG_DEFAULTS, "pilot_wait_s", 5)  # le defaut ne doit pas s'appliquer
    config = Config(mode="review", workspace_dir=env.config.workspace_dir, output_dir=env.config.output_dir,
                    _sections={"tiktok": {"content_check": "wait"}, "browser": {"pilot_wait_s": 0.2}})
    calls = {
        "publish": lambda: tiktok.publish(env.clip, "ma_chaine", mode="immediate", config=config, now=NOW),
        "fetch_stats": lambda: tiktok.fetch_stats("ma_chaine", config=config, now=NOW),
    }
    with browser._pilot_lock("compte_occupe", 1):  # un autre compte est en cours de pilotage
        for name, call in calls.items():
            started = time.monotonic()
            with pytest.raises(browser.BrowserError, match="compte_occupe"):
                call()
            assert time.monotonic() - started < 2, f"{name} : pilot_wait_s de config.toml ignoré"


def test_publications_date_without_year_uses_the_paris_year_at_new_year():
    """M3 (revue nuit) : le 31/12 23:30 UTC il est déjà le 1er janvier 00:30 à Paris, l'année est 2027."""
    months = _sel()["calendar"]["months"]
    now = datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc)
    assert tiktok.parse_date("1 janv., 00:10", months, now) == "2027-01-01T00:10:00"
    assert tiktok.parse_date("31 déc., 22:00", months, now) == "2026-12-31T22:00:00"
    # même instant, Paris encore en 2026 : le 31/12 22:30 UTC = 23:30 Paris
    before = datetime(2026, 12, 31, 22, 30, tzinfo=timezone.utc)
    assert tiktok.parse_date("31 déc., 23:00", months, before) == "2026-12-31T23:00:00"


def test_page_date_without_timezone_is_read_as_paris_time():
    # « 1er janvier 00:10 » à Paris = 31 décembre 23:10 UTC
    assert tiktok._naive_utc("2027-01-01T00:10:00") == datetime(2026, 12, 31, 23, 10, tzinfo=timezone.utc)


# -- onglets Spectateurs / Engagement et retention sur des extraits de la VRAIE page (TASK-429d, releve 2026-10-06)

_FIXTURES = Path(__file__).parent / "fixtures" / "tiktok"
_SIMPLE_TT = __import__("re").compile(r"\[data-tt='([^']+)'\]")


class _Dom(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
    """Arbre minimal d'un extrait HTML ; ``texts`` = lignes de texte (un noeud texte = une ligne, comme innerText)."""

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.nodes, self.stack = [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = {"tt": dict(attrs).get("data-tt"), "texts": []}
        self.nodes.append(node)
        self.stack.append(node)
        self.fresh = True

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()
        self.fresh = True

    def handle_data(self, data):  # un meme texte peut arriver en plusieurs morceaux (« &lt; » puis « 1% »)
        if not data.strip():
            return
        for node in self.stack:
            if self.fresh or not node["texts"]:
                node["texts"].append(data.strip())
            else:
                node["texts"][-1] += data.strip()
        self.fresh = False


def _real_cells(name, selector):
    """Les elements que ``selector`` (un simple ``[data-tt='X']``, le seul genre que l'extrait sait rejouer) trouve
    dans l'extrait ``name``, chacun avec son texte."""
    match = _SIMPLE_TT.fullmatch(selector)
    assert match, f"repère à confirmer sur la vraie page, pas un [data-tt='...'] exact : {selector!r}"
    source = (_FIXTURES / f"analytics_{name}.html").read_text(encoding="utf-8")
    return [FakeCell("\n".join(node["texts"])) for node in _Dom(source).nodes if node["tt"] == match[1]]


def _real_post(**kwargs):
    stats = _sel()["stats"]
    return Post(ID_A, viewers=_real_cells("viewers", stats["viewers_card"]),
                engagement=_real_cells("engagement", stats["engagement_card"]), **kwargs)


def test_the_selectors_of_the_three_unconfirmed_landmarks_are_no_longer_marked_to_verify():
    text = (Path(tiktok.__file__).parent / "assets" / "tiktok_selectors.toml").read_text(encoding="utf-8")
    for key in ("retention_point", "viewers_card", "engagement_card"):
        line = next(i for i, row in enumerate(text.splitlines()) if row.startswith(f"{key} = "))
        assert "A VERIFIER" not in text.splitlines()[line - 1].upper().replace("É", "E"), key


def test_the_viewers_cards_of_the_real_page_are_found_by_the_configured_landmark():
    cards = _real_cells("viewers", _sel()["stats"]["viewers_card"])

    assert [c.inner_text().splitlines()[0] for c in cards] == ["Total des spectateurs", "Types de spectateurs", "Âge", "Sexe", "Lieux"]


def test_the_viewers_tab_of_the_real_page_is_read_card_by_card(tmp_path, monkeypatch):
    got = StatsEnv(tmp_path, monkeypatch, [_real_post()]).fetch()["posts"][0]["viewers"]

    assert got["total"] == 1300  # « 1.3K »
    assert got["types"] == [{"label": "Nouveaux spectateurs", "value": 0.63}, {"label": "Spectateurs récurrents", "value": 0.37},
                            {"label": "Non followers", "value": 1.0}, {"label": "Followers", "value": 0.0}]
    assert got["age"][0] == {"label": "18-24", "value": 0.45} and len(got["age"]) == 5
    assert got["gender"] == [{"label": "Hommes", "value": 0.78}, {"label": "Femmes", "value": 0.2}, {"label": "Autre", "value": 0.02}]
    assert got["locations"][0] == {"label": "France", "value": 0.864}
    assert {"label": "Pays-bas", "value": None} in got["locations"]  # « <1% » : sous 1 %, jamais deviné


def test_the_engagement_tab_of_the_real_page_is_read_and_an_unfilled_card_is_a_null(tmp_path, monkeypatch):
    got = StatsEnv(tmp_path, monkeypatch, [_real_post()]).fetch()["posts"][0]

    assert got["engagement"]["comment_words"] is None  # « Tu pourras voir ces informations quand suffisamment de données... »


def test_the_retention_curve_of_the_real_page_is_a_chart_without_point_elements_so_it_is_a_null(tmp_path, monkeypatch):
    # releve reel : la rétention est un graphe echarts (canvas) sous une video, aucun element par point dans le DOM
    stats = _sel()["stats"]
    assert _real_cells("overview", stats["retention_point"]) == []
    got = StatsEnv(tmp_path, monkeypatch, [_real_post(curve=[])]).fetch()["posts"][0]

    assert got["retention_curve"] is None


# ---------------------------------------------------------------- relance de la vérification bloquée (TASK-aa57)


class RetriggerSwitch:
    """L'interrupteur « Vérification de contenu simple » en mode wait : coché au départ ; décocher puis recocher
    relance le scan (``on_check`` est appelé à chaque recochage)."""

    def __init__(self, *, disabled=False, sticky_off=False, on_check=None):
        self.checked, self.disabled, self.sticky_off, self.on_check = True, disabled, sticky_off, on_check
        self.events: list[str] = []

    def is_checked(self):
        return self.checked

    def is_disabled(self):
        return self.disabled

    def uncheck(self, **kwargs):
        self.events.append("uncheck")
        self.checked = False

    def check(self, **kwargs):
        self.events.append("check")
        if self.sticky_off:   # la case ne se recoche pas
            return
        self.checked = True
        if self.on_check is not None:
            self.on_check(len([e for e in self.events if e == "check"]))


def _stuck_env(tmp_path, monkeypatch, switch, **settings):
    env = Env(tmp_path, monkeypatch, page_kwargs={"check": "running"},
              settings={"poll_interval_s": 5, "content_check_retrigger_s": 10, **settings})
    real = env.page.query_selector
    sw = _sel()["selectors"]["content_check_switch"]
    env.page.query_selector = lambda css: switch if css == sw else real(css)
    return env


def test_a_stuck_content_check_is_retriggered_by_unchecking_then_rechecking_the_switch(tmp_path, monkeypatch, caplog):
    switch = RetriggerSwitch()
    env = _stuck_env(tmp_path, monkeypatch, switch)
    env.page.timeline = [None] * 3 + [lambda: env.page.set_check("ok")]   # fini 4 pas après le départ

    with caplog.at_level("INFO"):
        result = env.publish()

    assert switch.events == ["uncheck", "check"] and switch.checked is True
    assert env.page.posted == ["now"] and result["state"] == "published"
    assert any("relance 1/3" in r.getMessage() and "ma_chaine" in r.getMessage() for r in caplog.records)


def test_retriggers_are_capped_by_content_check_retriggers_then_the_wait_continues_to_the_timeout(tmp_path, monkeypatch):
    switch = RetriggerSwitch()
    env = _stuck_env(tmp_path, monkeypatch, switch, content_check_retriggers=2, content_check_timeout_s=100)

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert switch.events == ["uncheck", "check"] * 2
    assert stop.value.code == "content_check" and env.page.posted == []


def test_retriggers_stay_inside_the_content_check_timeout(tmp_path, monkeypatch):
    switch = RetriggerSwitch()
    env = _stuck_env(tmp_path, monkeypatch, switch, content_check_timeout_s=25)

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert switch.events == ["uncheck", "check"] * 2   # à 10 s et 20 s, pas de troisième avant 25 s
    assert stop.value.code == "content_check" and "25 s" in str(stop.value)


def test_a_check_finishing_before_the_delay_is_never_retriggered(tmp_path, monkeypatch):
    switch = RetriggerSwitch()
    env = _stuck_env(tmp_path, monkeypatch, switch, content_check_retrigger_s=60)
    env.page.timeline = [lambda: env.page.set_check("ok")]
    env.publish()
    assert switch.events == []


def test_a_greyed_switch_is_logged_and_the_wait_continues_without_touching_it(tmp_path, monkeypatch, caplog):
    switch = RetriggerSwitch(disabled=True)
    env = _stuck_env(tmp_path, monkeypatch, switch)
    env.page.timeline = [None] * 3 + [lambda: env.page.set_check("ok")]

    with caplog.at_level("INFO"):
        result = env.publish()

    assert switch.events == [] and result["state"] == "published"
    assert any("grisé" in r.getMessage() for r in caplog.records)


def test_a_missing_switch_is_logged_and_the_wait_continues(tmp_path, monkeypatch, caplog):
    # L'interrupteur est là au départ (allumé, TASK-0c44) puis disparaît pendant l'attente : la relance est
    # impossible, c'est journalisé et l'attente continue. Introuvable AU DÉPART : arrêt R4 (autre test).
    switch = RetriggerSwitch()
    env = _stuck_env(tmp_path, monkeypatch, None)
    sw = _sel()["selectors"]["content_check_switch"]
    gone = env.page.query_selector
    seen = []

    def present_once(css):
        if css == sw:
            seen.append(css)
            return switch if len(seen) == 1 else None
        return gone(css)

    env.page.query_selector = present_once
    env.page.timeline = [None] * 3 + [lambda: env.page.set_check("ok")]

    with caplog.at_level("INFO"):
        result = env.publish()

    assert result["state"] == "published"
    assert any("introuvable" in r.getMessage() for r in caplog.records)


def test_a_switch_left_unchecked_after_the_retrigger_stops_explicitly_and_nothing_is_posted(tmp_path, monkeypatch):
    switch = RetriggerSwitch(sticky_off=True)
    env = _stuck_env(tmp_path, monkeypatch, switch)

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "unexpected_page"
    assert env.page.posted == [] and _sel()["selectors"]["post_button"] not in env.page.clicks()


def test_a_problem_reported_after_a_retrigger_still_refuses_the_clip(tmp_path, monkeypatch):
    switch = RetriggerSwitch(on_check=lambda n: setattr(env.page, "timeline", [None] * env.page.waits + [lambda: env.page.set_check("problem")]))
    env = _stuck_env(tmp_path, monkeypatch, switch)
    env.page.timeline = [None] * 3

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "content_check_refused" and env.page.posted == []


def test_retrigger_settings_have_defaults_and_are_validated(tmp_path):
    assert tiktok.CONFIG_DEFAULTS["content_check_retrigger_s"] == 120
    assert tiktok.CONFIG_DEFAULTS["content_check_retriggers"] == 3
    for key, bad in (("content_check_retrigger_s", 0), ("content_check_retrigger_s", "120"),
                     ("content_check_retriggers", -1), ("content_check_retriggers", True)):
        config = Config(mode="review", workspace_dir=tmp_path, output_dir=tmp_path, _sections={"tiktok": {key: bad}})
        with pytest.raises(tiktok.TikTokError, match=key):
            tiktok.get_settings(config)


class RetryLink:
    """Le lien « Réessayer » sous l'erreur rouge : un clic relance le scan (``on_click``)."""

    def __init__(self, page, on_click=None):
        self.page, self.on_click, self.clicks = page, on_click, 0

    def click(self, **kwargs):
        self.clicks += 1
        if self.on_click is not None:
            self.on_click(self.clicks)


def _error_env(tmp_path, monkeypatch, switch=None, link=None, **settings):
    env = _stuck_env(tmp_path, monkeypatch, switch, **settings)
    sel = _sel()["selectors"]
    env.page.present.discard(sel["content_check_running"])
    env.page.present.add(sel["content_check_error"])
    env.page.texts[sel["content_check_error"]] = "Une erreur est survenue. Merci de réessayer plus tard."
    real = env.page.query_selector
    env.page.query_selector = lambda css: link if css == sel["content_check_retry"] and link else (
        None if css == sel["content_check_retry"] else real(css))
    return env


def test_a_tiktok_error_is_retried_by_clicking_reessayer_counted_and_logged(tmp_path, monkeypatch, caplog):
    env_ref = []

    def recovered(n):
        env_ref[0].page.present.discard(_sel()["selectors"]["content_check_error"])
        env_ref[0].page.set_check("ok")

    link = RetryLink(None, on_click=recovered)
    switch = RetriggerSwitch()
    env = _error_env(tmp_path, monkeypatch, switch, link)
    env_ref.append(env)

    with caplog.at_level("INFO"):
        result = env.publish()

    assert link.clicks == 1 and switch.events == []   # le lien, pas l'interrupteur
    assert result["state"] == "published" and env.page.posted == ["now"]
    assert any("Réessayer" in r.getMessage() and "relance 1/3" in r.getMessage() for r in caplog.records)


def test_an_error_without_a_reessayer_link_falls_back_to_unchecking_then_rechecking(tmp_path, monkeypatch):
    env_ref = []

    def recovered(n):
        env_ref[0].page.present.discard(_sel()["selectors"]["content_check_error"])
        env_ref[0].page.set_check("ok")

    switch = RetriggerSwitch(on_check=recovered)
    env = _error_env(tmp_path, monkeypatch, switch, None)
    env_ref.append(env)

    result = env.publish()

    assert switch.events == ["uncheck", "check"] and result["state"] == "published"


def test_an_error_still_shown_after_all_retries_stops_with_tiktoks_message(tmp_path, monkeypatch):
    link = RetryLink(None)
    env = _error_env(tmp_path, monkeypatch, RetriggerSwitch(), link, content_check_retriggers=2)

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert link.clicks == 2
    assert stop.value.code == "content_check" and "Une erreur est survenue" in str(stop.value)
    assert env.page.posted == []


def test_error_retries_share_the_budget_with_the_stuck_retriggers_and_stay_inside_the_timeout(tmp_path, monkeypatch):
    link = RetryLink(None)
    env = _error_env(tmp_path, monkeypatch, RetriggerSwitch(), link, content_check_timeout_s=25)

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert link.clicks == 3 and stop.value.code == "content_check"
    assert env.page.posted == []


# ---------------------------------------------------------------- TUXModal sans role=dialog (TASK-4b5a)


def _tux_modal(env, text="Activer les vérifications automatiques du contenu ? Annuler Activer", labels=("Annuler", "Activer")):
    """Constat reel du 08/10 : la fenetre est un TUXModal-overlay dans un portail, SANS role=dialog ni aria-modal."""
    return lambda: FakeModal(env.page, text, list(labels), attrs={"class": "TUXModal-overlay"})


def test_the_modal_container_selector_sees_a_tuxmodal_without_role_dialog():
    container = _sel()["modal"]["container"]
    assert _css_matches(container, {"class": "TUXModal-overlay"})
    assert _css_matches(container, {"role": "dialog"}) and _css_matches(container, {"aria-modal": "true"})
    assert not _css_matches(container, {"class": "Select__item"})


def test_a_tuxmodal_without_role_dialog_is_closed_the_click_redone_and_the_publication_continues(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch)
    post = _sel()["selectors"]["post_button"]
    _modal_between_guard_and_click(env, post, _tux_modal(env))

    with caplog.at_level("INFO"):
        result = env.publish()

    assert env.page.popups_closed == ["Annuler"]             # jamais « Activer »
    assert env.page.clicks().count(post) == 2
    assert env.page.posted == ["now"] and result["state"] == "published"
    assert set(env.page.click_timeouts) == {10_000}          # le bouton de la fenetre aussi : jamais les 30 s de Playwright


def test_cancelling_the_auto_checks_window_is_reported_when_content_check_is_wait(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch, settings={"content_check": "wait"})
    post = _sel()["selectors"]["post_button"]
    _modal_between_guard_and_click(env, post, _tux_modal(env))

    with caplog.at_level("WARNING"):
        env.publish()

    assert "vérification de contenu" in caplog.text.casefold() and "Annuler" in caplog.text


def test_an_unknown_tuxmodal_is_an_r4_stop_never_clicked(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    post = _sel()["selectors"]["post_button"]
    _modal_between_guard_and_click(env, post, _tux_modal(env, "Votre compte a été restreint OK", ["OK"]))

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "unexpected_page" and "Votre compte a été restreint" in str(stop.value)
    assert env.page.popups_closed == [] and env.page.clicks().count(post) == 1


def test_a_tuxmodal_that_comes_back_after_the_retry_is_an_r4_stop(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    post = _sel()["selectors"]["post_button"]
    _modal_between_guard_and_click(env, post, _tux_modal(env), times=2)

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.code == "unexpected_page" and env.page.clicks().count(post) == 2 and env.page.posted == []


# ---------------------------------------------------------------- captures sous [browser] state_dir (TASK-89dc)


def test_r4_capture_follows_the_browser_state_dir_setting(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, detect=["captcha"])
    state = tmp_path / "profils"
    env.config = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "output",
                        _sections={"tiktok": {"content_check": "wait"}, "browser": {"state_dir": str(state)}})

    with pytest.raises(tiktok.TikTokStop) as stop:
        env.publish()

    assert stop.value.capture.parent == state / "ma_chaine" / "captures" and stop.value.capture.is_file()


# -- TASK-05b430f5416e : deux liens de post a legende commune (tronquee) -> l'heure affichee designe le bon

_NOON_LINK = "https://www.tiktok.com/@ma_chaine/video/7300000000000000012"
_EVENING_LINK = "https://www.tiktok.com/@ma_chaine/video/7300000000000000019"
_EVENING_UTC = datetime(2026, 10, 3, 17, 30, tzinfo=timezone.utc)  # 19:30 a Paris (heure d'ete)


def _two_same_caption_links(env, *, created=("2026-10-03 12:00", "2026-10-03 19:30")):
    """Liste de Publications : le lien de 12:00 est d'abord, celui de 19:30 ensuite, meme texte tronque."""
    env.page.content_links = [("Ma legende #un", _NOON_LINK), ("Ma legende #un", _EVENING_LINK)]
    rows = [_row("7300000000000000012", caption="Ma legende #un #deux", created=created[0]),
            _row("7300000000000000019", caption="Ma legende #un #deux", created=created[1])]
    env.page.lists[_sel()["stats"]["row"]] = rows


def test_two_posts_with_the_same_caption_pick_the_link_whose_displayed_time_is_the_slot(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    _two_same_caption_links(env)

    result = env.publish(mode="scheduled", schedule_at=_EVENING_UTC)

    assert result["post_id"] == "7300000000000000019" and result["post_url"] == _EVENING_LINK
    assert result["note"] is None


def test_same_caption_without_a_readable_time_keeps_the_first_link_and_warns_once(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch)
    _two_same_caption_links(env)
    env.page.lists[_sel()["stats"]["row"]] = []  # heure non lisible sur la page

    with caplog.at_level("WARNING"):
        result = env.publish(mode="scheduled", schedule_at=_EVENING_UTC)

    assert result["post_id"] == "7300000000000000012"  # comportement d'origine : le premier lien
    assert caplog.text.count("portent la légende") == 1  # ambiguite journalisee, jamais devinee en silence


def test_same_caption_none_of_whose_times_is_the_slot_keeps_the_first_link_and_warns(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch)
    _two_same_caption_links(env, created=("2026-10-03 12:00", "2026-10-03 09:00"))

    with caplog.at_level("WARNING"):
        result = env.publish(mode="scheduled", schedule_at=_EVENING_UTC)

    assert result["post_id"] == "7300000000000000012"
    assert caplog.text.count("portent la légende") == 1


def test_a_unique_caption_link_is_taken_without_any_time_check_or_warning(tmp_path, monkeypatch, caplog):
    env = Env(tmp_path, monkeypatch, page_kwargs={"content_links": [("Ma legende #un", _EVENING_LINK)]})
    env.page.lists[_sel()["stats"]["row"]] = [_row("7300000000000000019", created="2026-10-03 09:00")]

    with caplog.at_level("WARNING"):
        result = env.publish(mode="scheduled", schedule_at=_EVENING_UTC)

    assert result["post_id"] == "7300000000000000019"
    assert "portent la légende" not in caplog.text


# ---------------------------------------------------------------- TASK-42a56837bbc6 : post programme loin dans la liste


class ScrollingStudioPage(StudioPage):
    """Page Publications virtualisee : les liens de post du DOM sont la fenetre courante de ``windows`` ; chaque
    ``evaluate(scroll_script)`` descend d'une fenetre (vrai = bas atteint). Les posts programmes en avance sont
    listes en premier (date decroissante) : le post tout juste programme est plus bas que la premiere fenetre."""

    def __init__(self, windows, **kwargs):
        super().__init__(**kwargs)
        self.windows, self.scrolled = windows, 0

    def goto(self, url, **kwargs):
        super().goto(url, **kwargs)
        self.scrolled = 0  # la page rechargee repart du haut

    def evaluate(self, script):
        self.calls.append(("evaluate", script))
        self.scrolled = min(self.scrolled + 1, len(self.windows) - 1)
        return self.scrolled == len(self.windows) - 1

    def wait_for_selector(self, selector, timeout=None, state=None):
        self.content_links = self.windows[self.scrolled]
        return super().wait_for_selector(selector, timeout=timeout, state=state)

    def query_selector_all(self, selector):
        self.content_links = self.windows[self.scrolled]
        return super().query_selector_all(selector)


def _scheduled_far_down_windows():
    ahead = [(f"Un autre clip programmé {n}", f"https://www.tiktok.com/@ma_chaine/video/73000000000000001{n:02d}")
             for n in range(15)]
    return [ahead[0:5], ahead[5:10], ahead[10:15], [("Ma legende #un #deux", LINK), ("Un clip plus ancien", OTHER_LINKS[0][1])]]


def test_a_scheduled_post_listed_below_the_first_rows_is_found_by_scrolling_the_list(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.page = ScrollingStudioPage(_scheduled_far_down_windows())

    result = env.publish(mode="scheduled", schedule_at=datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc))

    assert result["post_id"] == "7300000000000000001" and result["post_url"] == LINK
    assert result["state"] == "scheduled_on_tiktok" and "n'existe pas encore" not in (result["note"] or "")
    assert len([c for c in env.page.calls if c[0] == "evaluate"]) == 3
    assert not _lookup_polls(env.page, 10000)  # trouvé au premier essai : aucune relecture différée


def test_a_post_really_absent_from_the_whole_scrolled_list_stays_unfound(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, settings={"post_lookup_timeout_s": 0})
    windows = _scheduled_far_down_windows()[:3] + [OTHER_LINKS]
    env.page = ScrollingStudioPage(windows)

    result = env.publish(mode="scheduled", schedule_at=datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc))

    assert result["post_id"] is None and result["post_url"] is None  # jamais d'id inventé (ADR-ad2e)
    assert "n'existe pas encore" in result["note"]
    assert env.page.scrolled == len(windows) - 1  # toute la liste a été parcourue avant de conclure
