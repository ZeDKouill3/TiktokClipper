"""Comptes YouTube : socle de la publication par YouTube Studio (SPEC-5e50 R1, R5, R8 ; ADR-58c0).

Connexion et verification « pret a publier » (R1), puis publication immediate ou programmee (R2-R5, R8) ;
les statistiques viendront ensuite. Meme principe que TikTok : un vrai Chrome visible sur le profil persistant du compte
(``clipper.browser``), connexion Google faite a la main (Chrome NORMAL, jamais Playwright), aucun
identifiant saisi. Toutes les adresses et tous les reperes de la page vivent dans
``clipper/assets/youtube_selectors.toml``.

``verify_login`` ouvre YouTube Studio sur le profil : connecte quand l'adresse devient
``studio.youtube.com/channel/<UC...>/...`` et que la navigation affiche « Votre chaîne <nom> ». La page
de connexion Google, une page inattendue, un repere absent ou une fenetre inconnue rendent « pas pret »
avec la raison (et une capture), jamais un contournement ni un clic au hasard (R3, ADR-ad2e).

``publish`` envoie le mp4 par la dialog d'envoi de YouTube Studio (Details, Elements video, Verifications,
Visibilite) puis publie tout de suite ou programme cote YouTube. Toute date saisie est en heure de Paris (R8) :
le fuseau de la programmation est choisi explicitement (option « Paris », jamais « Heure locale ») et la date,
l'heure et le fuseau relus sur la page doivent correspondre, sinon arret. Captcha, verification Google, connexion
expiree, element absent apres delai, fenetre ou page inattendue : ``YouTubeStop`` (code, raison en francais,
capture sous ``state/browser/<compte>/captures/``), jamais un contournement ni un clic au hasard (R3).

Module d'etape isole : n'importe aucune autre etape ni ``clipper.web`` (ADR-b16b) ; seul ``clipper.browser``
(profil, verrou d'un seul compte pilote) est partage.
"""

from __future__ import annotations

import functools
import logging
import random
import re
import time
import tomllib
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from clipper import browser
from clipper.config import Config

logger = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, object] = {
    "min_action_delay_s": 0.3,   # pause humaine entre deux actions sur la page
    "max_action_delay_s": 1,
    "max_posts_per_day": 3,      # plafond par compte (R5)
    "min_gap_minutes": 120,      # ecart minimal entre deux publications d'un compte (R5)
    "action_timeout_s": 30,      # attente d'un element de la page (adresse de chaine, bouton...)
    "poll_interval_s": 1,        # pas d'attente entre deux lectures de la page
    "publish_mode": "immediate",  # immediate | scheduled (programme cote YouTube)
    "visibility": "public",      # public | unlisted | private
    "made_for_kids": False,      # « conçue pour les enfants » : non par defaut
    "upload_timeout_s": 600,     # attente de la fin de l'envoi du mp4 (lien du Short, bouton final actif)
    "publish_confirm_timeout_s": 120,  # attente de la fenetre « Vidéo mise en ligne » apres le clic final
    "schedule_max_days": 30,     # programmation la plus lointaine acceptee par Clipper (non releve sur la page)
    "schedule_min_minutes": 15,  # avance minimale d'une programmation (conservateur, non releve sur la page)
}

MODES = ("immediate", "scheduled")
VISIBILITIES = ("public", "unlisted", "private")
POST_OPTIONS = ("title", "visibility", "made_for_kids")
TITLE_MAX = 100              # limite de YouTube (compteur « n/100 » de l'etape Details)
REQUIRED_PUBLISH_LABELS = ("create", "upload_menu", "next", "kids_yes", "kids_no", "visibility_public",
                           "visibility_unlisted", "visibility_private", "expand_schedule", "timezone_button",
                           "timezone_wanted", "timezone_forbidden", "final_publish", "final_save", "final_schedule")
REQUIRED_PUBLISH_SELECTORS = ("file_input", "menu_item", "text_box", "radio", "labeled_button", "expand",
                              "text_input", "timezone_option", "final_button", "video_link")
_SHORTS_TAG = re.compile(r"(?<!\w)#shorts\b", re.IGNORECASE)

SELECTORS_PATH = Path(__file__).parent / "assets" / "youtube_selectors.toml"
MAX_POPUP_ROUNDS = 5   # fenetres successives fermees avant d'abandonner

Opener = Callable[..., AbstractContextManager]


class YouTubeError(Exception):
    """Reglage, clip, date ou option invalide ; fichier de reperes illisible."""


class YouTubeStop(YouTubeError):
    """Arret sur de la page (SPEC-5e50 R3). ``code`` : captcha | verification | login | element_missing |
    unexpected_page | publish_unconfirmed ; ``capture`` : la capture d'ecran, ou None."""

    def __init__(self, code: str, reason: str, capture: Path | None = None) -> None:
        super().__init__(reason)
        self.code, self.reason, self.capture = code, reason, capture


# ---------------------------------------------------------------- reglages et reperes


def get_settings(config: Config | None) -> dict[str, Any]:
    settings = dict(config.section("youtube")) if config is not None else dict(CONFIG_DEFAULTS)
    for key, allowed in (("publish_mode", MODES), ("visibility", VISIBILITIES)):
        if settings[key] not in allowed:
            raise YouTubeError(f"[youtube] {key} invalide : {settings[key]!r} (attendu : {' | '.join(allowed)})")
    if not isinstance(settings["made_for_kids"], bool):
        raise YouTubeError(f"[youtube] made_for_kids invalide : {settings['made_for_kids']!r} (true ou false attendu)")
    for key, minimum in (("max_posts_per_day", 1), ("min_gap_minutes", 0), ("min_action_delay_s", 0),
                         ("max_action_delay_s", 0), ("action_timeout_s", 1), ("poll_interval_s", 1),
                         ("upload_timeout_s", 1), ("publish_confirm_timeout_s", 1), ("schedule_max_days", 1),
                         ("schedule_min_minutes", 0)):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
            raise YouTubeError(f"[youtube] {key} invalide : {value!r} (un nombre >= {minimum} est attendu)")
    if settings["min_action_delay_s"] > settings["max_action_delay_s"]:
        raise YouTubeError(
            f"[youtube] min_action_delay_s ({settings['min_action_delay_s']}) dépasse max_action_delay_s "
            f"({settings['max_action_delay_s']})"
        )
    return settings


def load_selectors(path: str | Path | None = None) -> dict[str, Any]:
    """Adresses et reperes de YouTube Studio, valides ou ``YouTubeError`` qui nomme la cle."""
    target = Path(path) if path is not None else SELECTORS_PATH
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise YouTubeError(f"fichier de repères YouTube introuvable : {target}") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise YouTubeError(f"fichier de repères YouTube illisible ({target}) : {exc}") from exc

    def need(table: str, key: str, kind: type) -> None:
        value = data.get(table, {}).get(key)
        if kind is list:
            ok = isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value)
        else:
            ok = isinstance(value, kind) and bool(value)
        if not ok:
            raise YouTubeError(f"fichier de repères YouTube ({target.name}) : [{table}] {key} manquant ou invalide")

    need("urls", "studio", str)
    need("expect", "channel_url_pattern", str)
    need("expect", "login_url_markers", list)
    need("labels", "channel_prefix", str)
    need("selectors", "navigation", str)
    need("modal", "container", str)
    need("modal", "button", str)
    need("urls", "short", str)
    need("expect", "short_id_pattern", str)
    need("expect", "verification_url_markers", list)
    need("expect", "date_value_pattern", str)
    need("expect", "time_value_pattern", str)
    for key in REQUIRED_PUBLISH_LABELS:
        need("labels", key, str)
    for key in REQUIRED_PUBLISH_SELECTORS:
        need("selectors", key, str)
    for key in ("published", "scheduled", "saved"):
        need("success", key, list)
    need("detect", "captcha", list)
    need("detect", "verification", list)
    months = data.get("calendar", {}).get("months")
    if not (isinstance(months, list) and len(months) == 12 and all(isinstance(m, str) and m for m in months)):
        raise YouTubeError(f"fichier de repères YouTube ({target.name}) : [calendar] months manquant ou invalide "
                           f"(12 mois abrégés attendus)")
    try:
        for key in ("short_id_pattern", "date_value_pattern", "time_value_pattern"):
            re.compile(data["expect"][key])
    except re.error as exc:
        raise YouTubeError(f"fichier de repères YouTube ({target.name}) : [expect] {key} invalide ({exc})") from None
    if re.compile(data["expect"]["short_id_pattern"]).groups < 1:
        raise YouTubeError(f"fichier de repères YouTube ({target.name}) : [expect] short_id_pattern doit capturer "
                           f"l'identifiant de la vidéo (un groupe)")
    try:
        pattern = re.compile(data["expect"]["channel_url_pattern"])
    except re.error as exc:
        raise YouTubeError(f"fichier de repères YouTube ({target.name}) : [expect] channel_url_pattern "
                           f"invalide ({exc})") from None
    if pattern.groups < 1:
        raise YouTubeError(f"fichier de repères YouTube ({target.name}) : [expect] channel_url_pattern doit "
                           f"capturer l'identifiant de la chaîne (un groupe)")
    popups = data.get("popups")
    if not (isinstance(popups, dict) and popups and all(
            isinstance(k, str) and k and isinstance(v, str) and v and '"' not in v for k, v in popups.items())):
        raise YouTubeError(f"fichier de repères YouTube ({target.name}) : [popups] manquant ou invalide "
                           f"(texte de la fenêtre -> libellé du bouton, sans guillemet double)")
    return data


def studio_url(selectors: dict[str, Any] | None = None) -> str:
    """Page de connexion et de verification : YouTube Studio."""
    return str((selectors or load_selectors())["urls"]["studio"])


# ---------------------------------------------------------------- verification « pret a publier » (R1)


class _Check:
    """Une verification sur une page : chaque etape rend la raison de l'echec (jamais d'exception de page)."""

    def __init__(self, page: Any, account: str, sel: dict[str, Any], settings: dict[str, Any], *,
                 now: datetime, sleep: Callable[[float], None], rng: Any, config: Config | None = None) -> None:
        self.page, self.account, self.sel, self.settings = page, account, sel, settings
        self.now, self._sleep, self.rng, self.config = now, sleep, rng, config

    def pause(self) -> None:
        self._sleep(self.rng.uniform(float(self.settings["min_action_delay_s"]),
                                     float(self.settings["max_action_delay_s"])))

    def refuse(self, reason: str, *, capture: bool = False) -> dict[str, Any]:
        path: Path | None = None
        if capture:
            target = browser.profile_dir(self.account, self.config) / "captures" / f"{self.now.strftime('%Y%m%dT%H%M%S')}-youtube.png"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self.page.screenshot(path=str(target), full_page=True)
                path = target
            except Exception as exc:  # noqa: BLE001 - dit dans la raison, jamais avale
                reason += f" (capture d'écran impossible : {exc})"
        logger.warning("YouTube %s : pas prêt à publier : %s", self.account, reason)
        return {"ready": False, "reason": reason, "channel": None, "capture": str(path) if path else None}

    def wait_channel_url(self) -> tuple[str | None, str | None]:
        """Attend (borne) l'adresse de chaine. Rend ``(identifiant UC, None)``, ou ``(None, raison)`` pour la
        connexion Google ou une page inattendue."""
        pattern = re.compile(str(self.sel["expect"]["channel_url_pattern"]))
        markers = self.sel["expect"]["login_url_markers"]
        poll = float(self.settings["poll_interval_s"])
        rounds = max(1, int(float(self.settings["action_timeout_s"]) / poll))
        url = ""
        for attempt in range(rounds):
            url = str(self.page.url)
            if any(marker in url for marker in markers):
                return None, ("page de connexion Google affichée : connecte-toi à la main (Se connecter), "
                              "puis ferme la fenêtre")
            found = pattern.search(url)
            if found:
                return found.group(1), None
            if attempt < rounds - 1:
                self.page.wait_for_timeout(int(poll * 1000))
        return None, (f"page inattendue : YouTube Studio ne s'est pas ouvert sur une chaîne après "
                      f"{self.settings['action_timeout_s']:g} s (adresse : {url})")

    def close_popups(self) -> str | None:
        """Ferme les fenetres connues (``[popups]``) ; une fenetre inconnue ou sans son bouton rend la raison."""
        known = self.sel["popups"]
        for _ in range(MAX_POPUP_ROUNDS):
            shown = [m for m in self.page.query_selector_all(self.sel["modal"]["container"]) if m.is_visible()]
            if not shown:
                return None
            for modal in shown:
                text = " ".join(str(modal.inner_text()).split())
                fragment = next((f for f in known if f.casefold() in text.casefold()), None)
                if fragment is None:
                    return f"fenêtre inattendue : {text[:150]!r}"
                label = known[fragment]
                button = modal.query_selector(self.sel["modal"]["button"].format(label=label))
                if button is None:
                    return f"fenêtre connue « {fragment} » sans son bouton « {label} »"
                button.click()
                self.pause()
                logger.info("YouTube %s : fenêtre connue « %s » fermée par « %s »", self.account, fragment, label)
        return f"fenêtres surgissantes qui reviennent après {MAX_POPUP_ROUNDS} fermetures"

    def channel_name(self) -> tuple[str | None, str | None]:
        """Nom de la chaine : le texte « Votre chaîne <nom> » de la navigation."""
        prefix = str(self.sel["labels"]["channel_prefix"])
        # Vraie page (relevé 2026-10-03) : « Votre chaîne » puis le nom sur la ligne suivante ;
        # le nom sur la même ligne reste accepté.
        pattern = re.compile(re.escape(prefix) + r"[ \t ]*\n?[ \t ]*([^\n]*)", re.IGNORECASE)
        found = False
        for nav in self.page.query_selector_all(self.sel["selectors"]["navigation"]):
            match = pattern.search(str(nav.inner_text()))
            if match:
                found = True
                name = " ".join(match.group(1).split())
                if name:
                    return name, None
        if found:
            return None, f"nom de la chaîne illisible : « {prefix} » est affiché sans nom"
        return None, f"navigation sans « {prefix} <nom> » : la chaîne n'est pas affichée"


def verify_login(
    account: str, *, config: Config | None = None, selectors: dict[str, Any] | None = None,
    opener: Opener | None = None, sleep: Callable[[float], None] = time.sleep, rng: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Ouvre YouTube Studio sur le profil du compte (R1) et rend
    ``{"ready": bool, "reason": str | None, "channel": {"name", "id"} | None, "capture": chemin | None}``.
    Pret quand l'adresse est ``studio.youtube.com/channel/<UC...>/...`` et que la navigation affiche
    « Votre chaîne <nom> » (la fenetre « Bienvenue dans YouTube Studio » de la 1re visite est fermee par
    « Continuer »). Chrome ou Playwright absent : ``BrowserError`` (jamais de repli)."""
    browser.validate_account(account)
    settings = get_settings(config)
    sel = selectors or load_selectors()
    open_profile = opener or functools.partial(browser._open_context, config=config)  # pilot_wait_s du config
    with open_profile(account, headless=False) as context:  # visible : jamais de navigateur cache (ADR-58c0)
        page = browser.fresh_page(context)  # jamais context.pages[0] : onglet restauré par Chrome (profil Crashed)
        check = _Check(page, account, sel, settings, now=now or datetime.now(timezone.utc), sleep=sleep,
                       rng=rng or random.Random(), config=config)
        page.goto(str(sel["urls"]["studio"]))
        check.pause()
        channel_id, reason = check.wait_channel_url()
        if channel_id is None:
            return check.refuse(reason or "page inattendue", capture="inattendue" in (reason or ""))
        reason = check.close_popups()
        if reason is not None:
            return check.refuse(reason, capture=True)
        name, reason = check.channel_name()
        if name is None:
            return check.refuse(reason or "chaîne illisible", capture=True)
    logger.info("YouTube %s : prêt à publier, chaîne %s (%s)", account, name, channel_id)
    return {"ready": True, "reason": None, "channel": {"name": name, "id": channel_id}, "capture": None}


# ---------------------------------------------------------------- publication (R2-R5, R8)


def post_settings(settings: dict[str, Any], options: dict[str, Any] | None) -> dict[str, Any]:
    """Reglages d'une publication (R2) : ceux de [youtube] surcharges par ``options`` (titre, visibilite,
    « conçue pour les enfants »). Une option inconnue ou invalide est une erreur explicite."""
    merged = dict(settings)
    for key, value in (options or {}).items():
        if key not in POST_OPTIONS:
            raise YouTubeError(f"réglage de publication YouTube inconnu : {key!r} (attendu : {' | '.join(POST_OPTIONS)})")
        if key == "made_for_kids" and not isinstance(value, bool):
            raise YouTubeError(f"réglage de publication made_for_kids invalide : {value!r} (true ou false attendu)")
        if key == "visibility" and value not in VISIBILITIES:
            raise YouTubeError(f"réglage de publication visibility invalide : {value!r} "
                               f"(attendu : {' | '.join(VISIBILITIES)})")
        if key == "title" and (not isinstance(value, str) or not value.strip()):
            raise YouTubeError(f"réglage de publication title invalide : {value!r} (un texte non vide est attendu)")
        merged[key] = value
    return merged


def check_mode(mode: Any, settings: dict[str, Any]) -> None:
    """Mode valide et compatible avec la visibilite : YouTube ne programme que « comme publique »."""
    if mode not in MODES:
        raise YouTubeError(f"mode de publication invalide : {mode!r} (attendu : {' | '.join(MODES)})")
    if mode == "scheduled" and settings["visibility"] != "public":
        raise YouTubeError(
            f"programmation refusée : YouTube programme la vidéo « comme publique » (visibilité "
            f"{settings['visibility']!r} demandée) : choisis « Publique » ou publie en mode immédiat")


def clip_payload(sidecar: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
    """Le mp4, la legende, les hashtags et le titre d'ecran d'un sidecar de clip (SPEC-6a47)."""
    where = f"clip {sidecar.get('video_id')}/{sidecar.get('clip_id')}"
    if not isinstance(sidecar.get("caption"), str) or not sidecar["caption"].strip():
        raise YouTubeError(f"{where} : caption absente du sidecar")
    hashtags = sidecar.get("hashtags")
    if not isinstance(hashtags, list):
        raise YouTubeError(f"{where} : hashtags absents du sidecar")
    path = Path(output_dir) / sidecar["video_id"] / f"{sidecar['clip_id']}.mp4"
    return {"video_path": path, "caption": sidecar["caption"], "hashtags": list(hashtags),
            "screen_title": sidecar.get("screen_title") or ""}


def description_of(clip: dict[str, Any]) -> str:
    """Legende puis hashtags du sidecar, avec #Shorts ajoute s'il manque (R2)."""
    tags = [str(t) for t in clip["hashtags"]]
    if not _SHORTS_TAG.search(" ".join([clip["caption"], *tags])):
        tags.append("#Shorts")
    return clip["caption"] + ("\n\n" + " ".join(tags) if tags else "")


def _check_schedule(schedule_at: datetime | None, settings: dict[str, Any], now: datetime) -> None:
    if schedule_at is None or schedule_at.tzinfo is None:
        raise YouTubeError("publication programmée : une date avec fuseau horaire est requise")
    days, minutes = settings["schedule_max_days"], settings["schedule_min_minutes"]
    if schedule_at > now + timedelta(days=days):
        raise YouTubeError(f"programmation refusée : {schedule_at.isoformat()} dépasse la limite de {days} jours "
                           f"([youtube] schedule_max_days)")
    if schedule_at < now + timedelta(minutes=minutes):
        raise YouTubeError(f"programmation refusée : {schedule_at.isoformat()} est à moins de {minutes} minutes "
                           f"(avance minimale, [youtube] schedule_min_minutes) : publie en mode immédiat")


class _Flow:
    """Une publication pilotee sur une page. Chaque attente garde d'abord la page (connexion Google,
    verification, captcha) avant d'agir."""

    def __init__(self, page: Any, account: str, sel: dict[str, Any], settings: dict[str, Any], *,
                 now: datetime, sleep: Callable[[float], None], rng: Any, on_tick: Callable[[], None] | None,
                 config: Config | None = None) -> None:
        self.page, self.account, self.sel, self.settings = page, account, sel, settings
        self.now, self._sleep, self.rng, self.on_tick, self.config = now, sleep, rng, on_tick, config

    # -- arret sur (R3)
    def stop(self, code: str, reason: str) -> YouTubeStop:
        capture: Path | None = None
        target = browser.profile_dir(self.account, self.config) / "captures" / f"{self.now.strftime('%Y%m%dT%H%M%S')}-{code}.png"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(target), full_page=True)
            capture = target
        except Exception as exc:  # noqa: BLE001 - dit dans la raison, jamais avale
            reason += f" (capture d'écran impossible : {exc})"
        logger.error("YouTube %s : %s", self.account, reason)
        return YouTubeStop(code, reason, capture)

    def guard(self) -> None:
        url = str(self.page.url)
        expect = self.sel["expect"]
        if any(marker in url for marker in expect["verification_url_markers"]):
            raise self.stop("verification", "vérification de compte demandée par Google : arrêt immédiat, "
                                            f"à faire à la main (page : {url})")
        if any(marker in url for marker in expect["login_url_markers"]):
            raise self.stop("login", f"connexion expirée : reconnecte le compte {self.account} (page : {url})")
        for code, label in (("captcha", "captcha détecté : arrêt immédiat, à résoudre à la main"),
                            ("verification", "vérification de compte demandée : arrêt immédiat, à faire à la main")):
            for css in self.sel["detect"][code]:
                if self.page.query_selector(css) is not None:
                    raise self.stop(code, label)

    # -- actions
    def pause(self) -> None:
        if self.on_tick is not None:
            self.on_tick()
        self._sleep(self.rng.uniform(float(self.settings["min_action_delay_s"]),
                                     float(self.settings["max_action_delay_s"])))
        if self.on_tick is not None:
            self.on_tick()

    def wait_css(self, css: str, name: str, *, timeout_key: str = "action_timeout_s", state: str | None = None) -> Any:
        self.guard()
        timeout = float(self.settings[timeout_key])
        try:
            element = self.page.wait_for_selector(css, timeout=timeout * 1000, state=state)
        except Exception as exc:
            if "Timeout" not in type(exc).__name__:
                raise
            element = None
        if element is None:
            raise self.stop("element_missing", f"élément attendu absent après {timeout:g} s : {name}")
        return element

    def wait(self, name: str, **kwargs: Any) -> Any:
        return self.wait_css(self.sel["selectors"][name], name, **kwargs)

    def labeled(self, template: str, label_key: str) -> tuple[str, str]:
        label = str(self.sel["labels"][label_key])
        return self.sel["selectors"][template].format(label=label), label

    def click_labeled(self, template: str, label_key: str) -> None:
        css, label = self.labeled(template, label_key)
        self.wait_css(css, f"{template} « {label} »").click()
        self.pause()

    def wait_poll(self, until: Callable[[], Any], timeout_key: str) -> Any:
        """Lecture repetee de la page jusqu'a ``until`` (valeur non vide) ou ``timeout_key`` secondes ; rend la valeur
        ou None au delai. La page est gardee a chaque tour."""
        timeout, interval = float(self.settings[timeout_key]), float(self.settings["poll_interval_s"])
        waited = 0.0
        while True:
            self.guard()
            found = until()
            if found:
                return found
            if waited >= timeout:
                return None
            self.page.wait_for_timeout(interval * 1000)
            waited += interval
            if self.on_tick is not None:
                self.on_tick()

    # -- ouverture de la dialog d'envoi
    def open_upload_menu(self) -> None:
        """Clique « Créer » puis l'entree « Importer des videos » du menu : l'URL /videos/upload redirige vers le
        tableau de bord et n'ouvre jamais la dialog, elle n'est donc plus utilisee. Menu sans l'entree = arret
        explicite, jamais un autre clic au hasard."""
        self.click_labeled("labeled_button", "create")
        self.wait("menu_item")
        label = str(self.sel["labels"]["upload_menu"])
        items = [item for item in self.page.query_selector_all(self.sel["selectors"]["menu_item"])
                 if self.expect_text(item, label)]
        if not items:
            raise self.stop("unexpected_page", f"menu « {self.sel['labels']['create']} » sans l'entrée « {label} »")
        items[0].click()
        self.pause()

    def channel_id(self) -> str:
        self.page.goto(str(self.sel["urls"]["studio"]))
        self.pause()
        pattern = re.compile(str(self.sel["expect"]["channel_url_pattern"]))

        def read() -> str | None:
            found = pattern.search(str(self.page.url))
            return found.group(1) if found else None

        channel_id = self.wait_poll(read, "action_timeout_s")
        if channel_id is None:
            raise self.stop("unexpected_page", f"page inattendue : YouTube Studio ne s'est pas ouvert sur une chaîne "
                                               f"après {float(self.settings['action_timeout_s']):g} s "
                                               f"(adresse : {self.page.url})")
        return channel_id

    def close_popups(self) -> None:
        """Ferme les fenetres connues (``[popups]``) et le journalise ; la dialog d'envoi elle-meme (celle qui porte
        le champ de fichier) est ignoree ; toute autre fenetre est un arret (jamais de clic de repli)."""
        known = self.sel["popups"]
        for _ in range(MAX_POPUP_ROUNDS):
            shown = [m for m in self.page.query_selector_all(self.sel["modal"]["container"])
                     if m.is_visible() and m.query_selector(self.sel["selectors"]["file_input"]) is None]
            if not shown:
                return
            for modal in shown:
                text = " ".join(str(modal.inner_text()).split())
                fragment = next((f for f in known if f.casefold() in text.casefold()), None)
                if fragment is None:
                    raise self.stop("unexpected_page", f"fenêtre inattendue : {text[:150]!r}")
                label = known[fragment]
                button = modal.query_selector(self.sel["modal"]["button"].format(label=label))
                if button is None:
                    raise self.stop("element_missing", f"fenêtre connue « {fragment} » sans son bouton « {label} »")
                button.click()
                self.pause()
                logger.info("YouTube %s : fenêtre connue « %s » fermée par « %s »", self.account, fragment, label)
        raise self.stop("unexpected_page", f"fenêtres surgissantes qui reviennent après {MAX_POPUP_ROUNDS} fermetures")

    # -- etape Details
    def type_into(self, box: Any, text: str, *, clear: bool = True) -> None:
        box.click()
        if clear:
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Backspace")
        self.page.keyboard.insert_text(text)
        self.pause()

    def read_short_id(self) -> str:
        link = self.wait("video_link", timeout_key="upload_timeout_s")
        text = str(link.get_attribute("href") or link.inner_text())
        found = re.search(str(self.sel["expect"]["short_id_pattern"]), text)
        if not found:
            raise self.stop("unexpected_page", f"lien du Short illisible : {text[:150]!r}")
        return found.group(1)

    def fill_details(self, title: str, description: str) -> None:
        self.guard()
        self.wait("text_box")
        boxes = list(self.page.query_selector_all(self.sel["selectors"]["text_box"]))
        if len(boxes) < 2:
            raise self.stop("element_missing", f"{len(boxes)} zone(s) de texte affichée(s) (titre et description "
                                               f"attendus) : text_box")
        self.type_into(boxes[0], title)
        self.type_into(boxes[1], description)
        self.click_labeled("radio", "kids_yes" if self.settings["made_for_kids"] else "kids_no")

    # -- etape Visibilite
    def choose_visibility(self) -> None:
        self.click_labeled("radio", "visibility_" + str(self.settings["visibility"]))

    def expect_text(self, element: Any, wanted: str) -> bool:
        return wanted.casefold() in " ".join(str(element.inner_text()).split()).casefold()

    def choose_timezone(self) -> None:
        """Fuseau choisi explicitement : l'option dont le texte contient « Paris », jamais « Heure locale » (R8)."""
        labels = self.sel["labels"]
        wanted, forbidden = str(labels["timezone_wanted"]), str(labels["timezone_forbidden"])
        self.click_labeled("labeled_button", "timezone_button")
        self.wait("timezone_option")
        options = [o for o in self.page.query_selector_all(self.sel["selectors"]["timezone_option"])
                   if self.expect_text(o, wanted) and not self.expect_text(o, forbidden)]
        if not options:
            raise self.stop("element_missing", f"option de fuseau horaire « {wanted} » absente de la liste "
                                               f"(jamais « {forbidden} ») : timezone_option")
        options[0].click()
        self.pause()

    def schedule_fields(self) -> tuple[Any, Any]:
        """Champs date et heure de la section Programmer, reperes par leur valeur (les ids input-N sont instables)."""
        expect = self.sel["expect"]
        date_re, time_re = re.compile(str(expect["date_value_pattern"])), re.compile(str(expect["time_value_pattern"]))
        fields = list(self.page.query_selector_all(self.sel["selectors"]["text_input"]))
        date_field = next((f for f in fields if date_re.match(str(f.input_value()).strip())), None)
        time_field = next((f for f in fields if time_re.match(str(f.input_value()).strip())), None)
        if date_field is None or time_field is None:
            raise self.stop("unexpected_page", "champs de programmation inattendus : une date (« 4 oct. 2026 ») et une "
                                               "heure (« 00:00 ») sont attendues")
        return date_field, time_field

    def format_date(self, target: datetime) -> str:
        return f"{target.day} {self.sel['calendar']['months'][target.month - 1]} {target.year}"

    def parse_date(self, text: str) -> tuple[int, int, int] | None:
        found = re.match(r"^(\d{1,2})\s+(\S+?)\.?\s+(\d{4})$", text.strip())
        if not found:
            return None
        months = [m.rstrip(".").casefold() for m in self.sel["calendar"]["months"]]
        token = found.group(2).rstrip(".").casefold()
        if token not in months:
            return None
        return int(found.group(3)), months.index(token) + 1, int(found.group(1))

    def schedule_later(self, schedule_at: datetime) -> datetime:
        """Programmer : fuseau Paris, date puis heure saisies en heure de Paris, relecture (R8). Rend l'instant programme."""
        local = schedule_at.astimezone(ZoneInfo(browser.TIMEZONE))  # heure de Paris, jamais le fuseau du PC
        self.click_labeled("expand", "expand_schedule")
        self.choose_timezone()
        for kind, text in (("date", self.format_date(local)), ("time", local.strftime("%H:%M"))):
            field = self.schedule_fields()[0 if kind == "date" else 1]
            field.click()
            field.fill(text)
            field.press("Enter")
            self.pause()
        date_field, time_field = self.schedule_fields()
        shown_date, shown_time = str(date_field.input_value()).strip(), str(time_field.input_value()).strip()
        if self.parse_date(shown_date) != (local.year, local.month, local.day) or shown_time != local.strftime("%H:%M"):
            raise self.stop("unexpected_page", f"programmation non prise en compte : {shown_date} {shown_time} affiché, "
                                               f"{self.format_date(local)} {local.strftime('%H:%M')} (heure de Paris) attendu")
        css, label = self.labeled("labeled_button", "timezone_button")
        zone = " ".join(str(self.wait_css(css, f"labeled_button « {label} »").inner_text()).split())
        wanted, forbidden = str(self.sel["labels"]["timezone_wanted"]), str(self.sel["labels"]["timezone_forbidden"])
        if wanted.casefold() not in zone.casefold() or forbidden.casefold() in zone.casefold():
            raise self.stop("unexpected_page", f"fuseau horaire non pris en compte : {zone!r} affiché, « {wanted} » attendu")
        logger.info("YouTube %s : programmation relue : %s %s (heure de Paris)", self.account, shown_date, shown_time)
        return local

    # -- bouton final et preuve
    def press_final(self, expected_key: str) -> None:
        """Bouton final : actif (l'envoi du mp4 est fini), puis son libelle doit correspondre au mode."""
        css = self.sel["selectors"]["final_button"]
        self.wait("final_button")

        def enabled() -> Any:
            button = self.page.query_selector(css)
            return button if button is not None and button.is_enabled() else None

        button = self.wait_poll(enabled, "upload_timeout_s")
        if button is None:
            raise self.stop("element_missing", f"bouton final toujours inactif après "
                                               f"{float(self.settings['upload_timeout_s']):g} s : l'envoi du mp4 "
                                               f"n'est pas terminé ([youtube] upload_timeout_s) : final_button")
        expected = str(self.sel["labels"][expected_key])
        label = " ".join(str(button.inner_text()).split())
        if label != expected:
            raise self.stop("unexpected_page", f"bouton final « {label} » au lieu de « {expected} » : "
                                               f"la page n'est pas dans l'état voulu")
        button.click()
        self.pause()

    def await_success(self, titles: list[str]) -> None:
        """Preuve apres le clic final : une fenetre dont le texte contient un des titres attendus. Jamais un succes
        suppose : sans elle, arret ``publish_unconfirmed``."""
        container = self.sel["modal"]["container"]

        def seen() -> bool:
            for modal in self.page.query_selector_all(container):
                text = " ".join(str(modal.inner_text()).split()).casefold()
                if modal.is_visible() and any(t.casefold() in text for t in titles):
                    return True
            return False

        if not self.wait_poll(seen, "publish_confirm_timeout_s"):
            raise self.stop("publish_unconfirmed",
                            f"aucune fenêtre « {' » ou « '.join(titles)} » après "
                            f"{float(self.settings['publish_confirm_timeout_s']):g} s ([youtube] "
                            f"publish_confirm_timeout_s) ; la vidéo est peut-être en ligne, à vérifier à la main "
                            f"dans YouTube Studio avant de réessayer")

    def run(self, clip: dict[str, Any], title: str, mode: str, schedule_at: datetime | None) -> dict[str, Any]:
        sel, settings = self.sel, self.settings
        self.channel_id()
        self.close_popups()
        self.open_upload_menu()

        self.wait("file_input", state="attached")
        self.page.set_input_files(sel["selectors"]["file_input"], str(clip["video_path"]))
        self.pause()
        short_id = self.read_short_id()
        self.fill_details(title, description_of(clip))
        for _ in range(3):  # Details, Elements video, Verifications
            self.click_labeled("labeled_button", "next")

        visibility = str(settings["visibility"])
        effective: datetime | None = None
        if mode == "scheduled":
            effective = self.schedule_later(schedule_at)
            final, titles = "final_schedule", list(sel["success"]["scheduled"])
        else:
            self.choose_visibility()
            private = visibility == "private"
            final, titles = ("final_save", list(sel["success"]["saved"])) if private else (
                "final_publish", list(sel["success"]["published"]))
        self.press_final(final)
        self.await_success(titles)
        return self.result(short_id, mode, effective, visibility)

    def result(self, short_id: str, mode: str, effective: datetime | None, visibility: str) -> dict[str, Any]:
        note = {"private": "vidéo publiée en privé", "unlisted": "vidéo non répertoriée"}.get(visibility)
        scheduled = mode == "scheduled"
        return {
            "post_url": str(self.sel["urls"]["short"]).format(id=short_id),
            "post_id": short_id,
            "state": "scheduled_on_youtube" if scheduled else "published",
            "publish_at": (effective if scheduled else self.now).isoformat(),
            "note": None if scheduled else note,
        }


def publish(
    clip: dict[str, Any], account: str, *, mode: str | None = None, schedule_at: datetime | None = None,
    config: Config | None = None, now: datetime | None = None, selectors: dict[str, Any] | None = None,
    opener: Opener | None = None, sleep: Callable[[float], None] = time.sleep, rng: Any = None,
    on_tick: Callable[[], None] | None = None, options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publie ``clip`` ({video_path, caption, hashtags, screen_title}) sur le compte YouTube, avec les reglages
    ``options`` de cette publication (titre, visibilite, enfants ; sinon ceux de [youtube]), ``mode`` ``immediate``
    ou ``scheduled`` (date ``schedule_at``, programmation cote YouTube, heure de Paris). Rend
    ``{post_url, post_id, state, publish_at, note}`` ; leve ``YouTubeError`` (reglage, date, clip), ``YouTubeStop`` (R3)
    ou ``BrowserError`` (Chrome/Playwright absent)."""
    settings = post_settings(get_settings(config), options)
    mode = mode if mode is not None else str(settings["publish_mode"])
    if not account:
        raise YouTubeError("compte YouTube manquant : la publication n'a pas de compte")
    check_mode(mode, settings)
    if not Path(clip["video_path"]).is_file():
        raise YouTubeError(f"mp4 introuvable : {clip['video_path']}")
    title = " ".join(str((options or {}).get("title") or clip.get("screen_title") or "").split())
    if not title:
        raise YouTubeError("titre manquant : ni titre de publication ni titre d'écran dans le sidecar du clip")
    if len(title) > TITLE_MAX:
        logger.warning("YouTube %s : titre tronqué à %d caractères (%d)", account, TITLE_MAX, len(title))
        title = title[:TITLE_MAX].rstrip()
    now = now or datetime.now(timezone.utc)
    if mode == "scheduled":
        _check_schedule(schedule_at, settings, now)
    browser.validate_account(account)
    sel = selectors or load_selectors()
    open_profile = opener or functools.partial(browser._open_context, config=config)  # pilot_wait_s du config
    with open_profile(account, headless=False) as context:  # visible : jamais de navigateur cache (ADR-58c0)
        page = browser.fresh_page(context)  # jamais context.pages[0] : onglet restauré par Chrome (profil Crashed)
        flow = _Flow(page, account, sel, settings, now=now, sleep=sleep, rng=rng or random.Random(), on_tick=on_tick,
                     config=config)
        try:
            return flow.run(clip, title, mode, schedule_at)
        except YouTubeStop:
            raise
        except Exception as exc:  # noqa: BLE001 - erreur Playwright : arret sur avec capture
            raise flow.stop("unexpected_page", f"page inattendue : {type(exc).__name__} : {exc}") from exc
