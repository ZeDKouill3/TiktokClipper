"""Profils de navigateur persistants par compte (ADR-1a58, SPEC-9225 R1, R8).

Un profil vit dans ``state/browser/<compte>/`` (ignore par git, jamais copie hors de
``state/``). « Se connecter » lance un Chrome NORMAL en sous-processus sur ce profil
(jamais Playwright : TikTok refuse la connexion d'un Chrome pilote) et attend sa
fermeture ; l'utilisateur se connecte a la main, aucune fonction d'ici ne saisit
d'identifiant ni de mot de passe. Playwright reutilise ensuite la session du profil
pour publier et lire les statistiques (``_open_context``). Les cookies YouTube d'un profil s'exportent
vers ``cookies.txt`` (format Netscape) pour yt-dlp.

Pas de repli (ADR-ad2e) : Playwright ou Chrome absent, profil absent, aucun
cookie a exporter = ``BrowserError`` en francais avec la commande a lancer.

Module isole : n'importe aucune etape ni ``clipper.web`` (ADR-b16b). Playwright
est importe a la demande ; les tests branchent un faux avec ``use_playwright``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from clipper import accounts, network
from clipper.config import Config

logger = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, object] = {
    # Page ouverte par « clipper browser login <compte> » sans --url.
    "login_url": "https://www.tiktok.com/login",
    # Domaines dont les cookies sortent du profil vers cookies.txt (yt-dlp) ;
    # les autres cookies du profil (TikTok...) ne sont jamais exportes.
    "cookie_domains": ["youtube.com", "google.com"],
    # Chemin de chrome.exe pour « se connecter » ; vide = detection (PATH, emplacements usuels).
    "chrome_path": "",
    # Delai (s) pour que la fenetre s'ouvre quand la console la demande.
    "launch_timeout_s": 60,
    # Etat de connexion TikTok d'un profil (SPEC-00d1 R2) : cookies lus localement, sans navigation.
    # Un profil est connecte quand un de ces cookies de session existe, sur l'un de ces domaines.
    "login_domains": ["tiktok.com"],
    "login_cookies": ["sessionid", "sessionid_ss"],
    # Un seul compte piloté à la fois, tous services confondus (ADR-58c0) : attente (s) du compte en cours
    # avant d'abandonner avec une erreur explicite.
    "pilot_wait_s": 300,
    # Dossier des profils Chrome (un sous-dossier par compte) ; verrou et detenteur du pilotage dans son parent.
    "state_dir": "state/browser",
}

COOKIES_FILE = "cookies.txt"
CHANNEL = "chrome"  # le vrai Chrome, jamais un Chromium embarque (SPEC-9225 R1)
TIMEZONE = "Europe/Paris"  # fuseau de tout contexte piloté (SPEC-5e50 R8), jamais celui du PC
INSTALL_PLAYWRIGHT = "uv pip install playwright"
INSTALL_CHROME = "playwright install chrome"

_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NETSCAPE_HEADER = "# Netscape HTTP Cookie File"

_override: Callable[[], Any] | None = None
_cookie_reader: Callable[[str], list[dict[str, Any]]] | None = None  # remplace par les tests : aucun vrai profil
_popen: Callable[..., Any] = subprocess.Popen  # remplace par les tests : aucun vrai Chrome
_lock = threading.Lock()
_active: dict[str, Any] = {}  # comptes dont la fenetre de connexion est ouverte
_pilot = threading.Lock()  # fils d'un meme processus ; entre processus : state/pilot.lock (_pilot_lock)
_PILOT_POLL_S = 0.05


class BrowserError(Exception):
    """Compte ou URL invalide, Playwright/Chrome absent, profil absent, export impossible."""


class BrowserUnavailable(BrowserError):
    """Navigateur refusé pour une condition transitoire (pays de l'IP inconnu) : à réessayer, pas un arrêt sûr."""


def use_playwright(factory: Callable[[], Any] | None) -> None:
    """Branche une fabrique ``sync_playwright`` (tests : faux) ; None = le vrai."""
    global _override
    _override = factory


def use_cookie_reader(reader: Callable[[str], list[dict[str, Any]]] | None) -> None:
    """Branche une lecture de cookies (tests : simulee) ; None = la base de cookies du profil."""
    global _cookie_reader
    _cookie_reader = reader


def _playwright_factory() -> Callable[[], Any]:
    if _override is not None:
        return _override
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BrowserError(
            "Playwright n'est pas installé : lance « " + INSTALL_PLAYWRIGHT + " » "
            "(ou relance tools/setup.ps1), puis « " + INSTALL_CHROME + " » si Chrome manque"
        ) from None
    return sync_playwright


def _settings(config: Config | None) -> dict[str, object]:
    return config.section("browser") if config is not None else dict(CONFIG_DEFAULTS)


# ---------------------------------------------------------------- profil


def validate_account(account: object) -> str:
    """Id de compte sur pour un nom de dossier : ni separateur, ni point, ni traversee."""
    if not isinstance(account, str) or not _ACCOUNT_ID.fullmatch(account):
        raise BrowserError(
            f"identifiant de compte invalide : {account!r} (attendu : 1 à 64 caractères parmi lettres, chiffres, _ et -)"
        )
    return account


def _state_dir(config: Config | None) -> Path:
    return Path(str(_settings(config)["state_dir"]))


def profile_dir(account: object, config: Config | None = None) -> Path:
    return _state_dir(config) / validate_account(account)


def profile_status(account: object, config: Config | None = None) -> dict[str, Any]:
    """``{"present": bool, "modified_at": ISO UTC | None}`` : un profil existe quand son
    dossier contient quelque chose (Chrome y ecrit des l'ouverture)."""
    directory = profile_dir(account, config)
    try:
        times = [p.stat().st_mtime for p in directory.iterdir()]
    except FileNotFoundError:
        return {"present": False, "modified_at": None}
    except OSError as exc:
        raise BrowserError(f"profil illisible : {directory} ({type(exc).__name__})") from None
    if not times:
        return {"present": False, "modified_at": None}
    stamp = datetime.fromtimestamp(max(times), tz=timezone.utc).isoformat(timespec="seconds")
    return {"present": True, "modified_at": stamp}


# ---------------------------------------------------------------- connexion TikTok (SPEC-00d1 R2)

LOGIN_STATES = ("never", "connected", "expired")
_COOKIE_FILES = (Path("Default") / "Network" / "Cookies", Path("Default") / "Cookies")
_CHROME_EPOCH_S = 11_644_473_600  # 1601-01-01 -> 1970-01-01


def _read_profile_cookies(account: str, config: Config | None = None) -> list[dict[str, Any]]:
    """Cookies du profil lus dans la base SQLite de Chrome (copie temporaire, lecture seule) : nom,
    domaine, expiration (secondes Unix, -1 pour un cookie de session). Les valeurs ne sont jamais
    lues (chiffrees par Chrome, inutiles ici) ; rien n'est lance, rien n'est envoye."""
    directory = profile_dir(account, config)
    source = next((directory / name for name in _COOKIE_FILES if (directory / name).is_file()), None)
    if source is None:
        return []
    with tempfile.TemporaryDirectory(prefix="clipper-cookies-") as tmp:
        copy = Path(tmp) / "Cookies"
        try:
            shutil.copyfile(source, copy)
        except OSError as exc:
            raise BrowserError(
                f"cookies du profil {account} illisibles ({type(exc).__name__}) : ferme la fenêtre Chrome "
                "de ce compte puis réessaie"
            ) from None
        try:
            db = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
            try:
                rows = db.execute("SELECT host_key, name, expires_utc, is_persistent FROM cookies").fetchall()
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise BrowserError(f"base de cookies du profil {account} illisible : {type(exc).__name__}") from None
    cookies = []
    for host, name, expires_utc, persistent in rows:
        expires = expires_utc / 1_000_000 - _CHROME_EPOCH_S if persistent and expires_utc else -1
        cookies.append({"domain": host, "name": name, "expires": expires})
    return cookies


def login_state(account: str, *, config: Config | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Etat de connexion TikTok du profil (R2), lu dans ses cookies sans naviguer :
    ``{"state": "never" | "connected" | "expired", "checked_at": ISO UTC, "expires_at": ISO | None}``.
    ``never`` : pas de profil ou aucun cookie de session TikTok ; ``connected`` : un cookie de session
    non expire (``expires_at`` None pour un cookie de session du navigateur) ; ``expired`` : les cookies
    de session TikTok sont la mais tous expires."""
    validate_account(account)
    settings = _settings(config)
    domains = [str(d).lower() for d in settings["login_domains"]]
    names = {str(n) for n in settings["login_cookies"]}
    moment = now or datetime.now(timezone.utc)
    stamp = moment.isoformat(timespec="seconds")
    if not profile_status(account, config)["present"]:
        return {"state": "never", "checked_at": stamp, "expires_at": None}
    reader = _cookie_reader or (lambda name: _read_profile_cookies(name, config))
    session = [c for c in reader(account) if c.get("name") in names and _in_domains(str(c.get("domain", "")), domains)]
    if not session:
        return {"state": "never", "checked_at": stamp, "expires_at": None}
    now_s = moment.timestamp()
    alive = [c for c in session if float(c.get("expires") or -1) <= 0 or float(c["expires"]) > now_s]
    if not alive:
        return {"state": "expired", "checked_at": stamp, "expires_at": None}
    if any(float(c.get("expires") or -1) <= 0 for c in alive):
        return {"state": "connected", "checked_at": stamp, "expires_at": None}
    last = max(float(c["expires"]) for c in alive)
    return {"state": "connected", "checked_at": stamp,
            "expires_at": datetime.fromtimestamp(last, tz=timezone.utc).isoformat(timespec="seconds")}


# ---------------------------------------------------------------- ouverture


def _is_missing_chrome(message: str) -> bool:
    low = message.lower()
    return "distribution" in low and "not found" in low or "playwright install" in low or "executable doesn't exist" in low


def _pilot_paths(config: Config | None = None) -> tuple[Path, Path]:
    """Verrou de pilotage partage par tous les processus (worker, serveur web, CLI) : ``state/pilot.lock``, et
    ``state/pilot.json`` qui nomme le compte pilote (message d'attente)."""
    root = _state_dir(config).parent
    return root / "pilot.lock", root / "pilot.json"


def _try_lock(handle: Any) -> bool:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: Any) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _pilot_holder(config: Config | None = None) -> str:
    """Compte pilote par le detenteur du verrou, d'apres ``state/pilot.json`` ; sinon dit pourquoi on l'ignore."""
    _, holder_path = _pilot_paths(config)
    try:
        holder = json.loads(holder_path.read_text(encoding="utf-8"))
        return f"{holder['account']} (processus {holder['pid']})"
    except FileNotFoundError:
        return "(compte non encore inscrit par l'autre processus)"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"(compte illisible dans {holder_path} : {type(exc).__name__})"


@contextmanager
def _pilot_lock(account: str, wait: float, config: Config | None = None) -> Iterator[None]:
    """Un seul compte piloté à la fois, tous services et tous processus confondus (ADR-58c0) : verrou de fil
    (fils d'un même processus) puis verrou de fichier ``state/pilot.lock`` (worker et serveur web), le tout
    en ``wait`` secondes au plus ; au-delà, ``BrowserError`` qui nomme le compte piloté."""
    deadline = time.monotonic() + wait

    def refuse(holder: str) -> BrowserError:
        return BrowserError(
            f"le compte {holder} est déjà piloté (un seul compte à la fois, tous services confondus) : "
            f"attente de {wait:g} s dépassée pour {account}, réessaie quand il a fini"
        )

    if not _pilot.acquire(timeout=wait):
        raise refuse(_pilot_holder(config))
    try:
        lock_path, holder_path = _pilot_paths(config)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    raise refuse(_pilot_holder(config))
                time.sleep(_PILOT_POLL_S)
            try:
                tmp = holder_path.with_name(f"{holder_path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps({"account": account, "pid": os.getpid(),
                                           "since": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
                os.replace(tmp, holder_path)
                yield
            finally:
                try:
                    holder_path.unlink()
                except OSError as exc:
                    logger.warning("%s non effacé : %s", holder_path, exc)
                _unlock(handle)
    finally:
        _pilot.release()


@contextmanager
def _open_context(account: str, *, headless: bool, config: Config | None = None) -> Iterator[Any]:
    """Contexte Playwright du profil, en heure de Paris (R8). Un seul compte est piloté à la fois, tous
    services et tous processus confondus : un second appel attend ``[browser] pilot_wait_s`` puis échoue
    explicitement. Hors du pays attendu (``[network]``, TASK-30cc) ou pays inconnu : refus avant toute ouverture."""
    directory = profile_dir(account, config)
    try:
        expected, label = accounts.country_guard(config, account) if config is not None else (None, None)
        network.require_expected_country(config, expected=expected, account=label if expected else None)
    except network.NetworkUnknown as exc:
        raise BrowserUnavailable(str(exc)) from None
    except network.NetworkError as exc:
        raise BrowserError(str(exc)) from None
    wait = float(_settings(config)["pilot_wait_s"])
    with _pilot_lock(account, wait, config):
        manager = _playwright_factory()()
        pw = manager.start()
        try:
            try:
                context = pw.chromium.launch_persistent_context(
                    str(directory), channel=CHANNEL, headless=headless, no_viewport=True, timezone_id=TIMEZONE,
                    ignore_default_args=["--enable-automation"],
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception as exc:  # noqa: BLE001 - erreur Playwright : retraduite, jamais avalee
                message = str(exc)
                if _is_missing_chrome(message):
                    raise BrowserError(
                        "Chrome est introuvable : installe Google Chrome, ou lance « " + INSTALL_CHROME + " »"
                    ) from None
                raise BrowserError(f"ouverture du navigateur impossible : {message.strip().splitlines()[0] if message.strip() else type(exc).__name__}") from None
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'languages', {get: () => ['fr-FR', 'fr', 'en']});
                window.chrome = window.chrome || { runtime: {} };
            """)
            try:
                yield context
            finally:
                try:
                    context.close()
                except Exception as exc:  # noqa: BLE001 - fenetre deja fermee par l'utilisateur
                    logger.debug("fermeture du contexte : %s", type(exc).__name__)
        finally:
            pw.stop()


def fresh_page(context: Any) -> Any:
    """Onglet neuf du contexte, pour piloter une page. Jamais ``context.pages[0]`` : sur un profil dont la
    session précédente a été tuée (``exit_type`` = Crashed, serveur relancé avec Chrome ouvert), Chrome
    restaure puis referme des onglets au démarrage, et l'onglet préexistant disparaît en plein envoi
    (« Target page, context or browser has been closed »). Les onglets restaurés sont fermés après
    l'ouverture de la page neuve, jamais avant (fermer le dernier onglet fermerait Chrome)."""
    stale = list(context.pages)
    page = context.new_page()
    for old in stale:
        if old is page:
            continue
        try:
            old.close()
        except Exception as exc:  # noqa: BLE001 - onglet déjà refermé par Chrome : c'est le but
            logger.debug("fermeture d'un onglet restauré : %s", type(exc).__name__)
    return page


def _login_url(url: str | None, config: Config | None) -> str:
    value = url if url is not None else str(_settings(config)["login_url"])
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BrowserError(f"URL invalide : {value!r} (une adresse http(s) complète est attendue)")
    return value


def _chrome_candidates() -> list[Path]:
    """Emplacements usuels d'un Google Chrome installe (Windows, macOS, Linux)."""
    found = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(var)
        if root:
            found.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    found += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
              Path("/opt/google/chrome/chrome")]
    return found


def find_chrome(config: Config | None = None) -> Path:
    """Le Chrome normal a lancer pour la connexion : ``[browser] chrome_path`` s'il est regle (introuvable
    = erreur, pas de repli), sinon le PATH puis les emplacements usuels ; sinon ``BrowserError``."""
    configured = str(_settings(config)["chrome_path"]).strip()
    if configured:
        path = Path(configured)
        if not path.is_file():
            raise BrowserError(f"Chrome introuvable : [browser] chrome_path = {configured!r} n'existe pas")
        return path
    for name in ("chrome", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in _chrome_candidates():
        if candidate.is_file():
            return candidate
    raise BrowserError(
        "Chrome est introuvable : installe Google Chrome (https://www.google.com/chrome), "
        "ou renseigne [browser] chrome_path dans config.toml avec le chemin de chrome.exe"
    )


def login(
    account: str, url: str | None = None, *, config: Config | None = None,
    on_open: Callable[[], None] | None = None,
) -> None:
    """Lance un Chrome normal en sous-processus sur le profil du compte, page de connexion, et attend
    sa fermeture. L'utilisateur se connecte lui-meme ; aucun pilotage, aucun drapeau d'automatisation
    (TikTok refuse la connexion d'un Chrome lance par Playwright)."""
    validate_account(account)
    target = _login_url(url, config)
    chrome = find_chrome(config)
    directory = profile_dir(account, config)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        process = _popen([str(chrome), f"--user-data-dir={directory.resolve()}", "--no-first-run", target])
    except OSError as exc:
        raise BrowserError(f"Chrome n'a pas pu être lancé ({chrome}) : {exc}") from None
    if on_open is not None:
        on_open()
    process.wait()


def start_login(
    account: str, url: str | None = None, *, config: Config | None = None,
    on_close: Callable[[], None] | None = None,
) -> None:
    """``login`` dans un fil, pour la console : rend la main une fois Chrome lance
    (ou leve ``BrowserError`` s'il n'a pas pu l'etre). Une seule fenetre par profil. ``on_close`` est
    appele une fois la fenetre fermee (la console y verifie la connexion, SPEC-00d1 R2)."""
    validate_account(account)
    _login_url(url, config)
    find_chrome(config)
    timeout = float(_settings(config)["launch_timeout_s"])
    ready = threading.Event()
    failure: list[BrowserError] = []

    def run() -> None:
        try:
            login(account, url, config=config, on_open=ready.set)
        except BrowserError as exc:
            failure.append(exc)
        except Exception as exc:  # noqa: BLE001
            failure.append(BrowserError(f"connexion interrompue : {type(exc).__name__}"))
        else:
            with _lock:
                _active.pop(account, None)  # fenetre fermee : le profil est libre pour la verification
            if on_close is not None:
                try:
                    on_close()
                except Exception:  # noqa: BLE001 - la verification echoue seule, jamais le fil de connexion
                    logger.exception("verification de la connexion de %s apres fermeture", account)
        finally:
            with _lock:
                _active.pop(account, None)
            ready.set()

    thread = threading.Thread(target=run, name=f"browser-login-{account}", daemon=True)
    with _lock:
        if account in _active:
            raise BrowserError(f"la fenêtre de connexion du compte {account} est déjà ouverte")
        _active[account] = thread
    thread.start()
    if not ready.wait(timeout):
        raise BrowserError(f"le navigateur ne s'est pas ouvert dans les {timeout:g} s")
    if failure:
        raise failure[0]


# ---------------------------------------------------------------- cookies


def _in_domains(domain: str, suffixes: list[str]) -> bool:
    host = domain.lstrip(".").lower()
    return any(host == s or host.endswith("." + s) for s in suffixes)


def format_netscape(cookies: list[dict[str, Any]]) -> str:
    """Cookies Playwright -> texte au format Netscape lu par yt-dlp (``#HttpOnly_`` pour
    les cookies HttpOnly, expiration 0 pour un cookie de session)."""
    lines = [_NETSCAPE_HEADER, ""]
    for c in cookies:
        name, value, domain = str(c["name"]), str(c["value"]), str(c["domain"])
        if any(ch in field for field in (name, value, domain) for ch in "\t\r\n"):
            raise BrowserError(f"cookie {name!r} inexportable : tabulation ou saut de ligne dans ses champs")
        expires = int(c.get("expires") or -1)
        fields = [
            ("#HttpOnly_" if c.get("httpOnly") else "") + domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            str(c.get("path") or "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(expires if expires > 0 else 0),
            name,
            value,
        ]
        lines.append("\t".join(fields))
    return "\n".join(lines) + "\n"


def export_cookies(account: str, *, config: Config | None = None) -> Path:
    """Exporte les cookies YouTube/Google du profil vers ``state/browser/<compte>/cookies.txt``
    (ecriture atomique, lisible par le seul utilisateur) et rend son chemin."""
    directory = profile_dir(account, config)
    if account in _active:
        raise BrowserError(
            f"la fenêtre de connexion du compte {account} est encore ouverte : ferme-la avant d'exporter les cookies"
        )
    if not profile_status(account, config)["present"]:
        raise BrowserError(
            f"profil absent pour le compte {account} : connecte-toi d'abord avec "
            f"« clipper browser login {account} --url https://www.youtube.com »"
        )
    suffixes = [str(d).lower() for d in _settings(config)["cookie_domains"]]
    with _open_context(account, headless=True, config=config) as context:
        cookies = [c for c in context.cookies() if _in_domains(str(c.get("domain", "")), suffixes)]
    if not cookies:
        raise BrowserError(
            f"aucun cookie {' / '.join(suffixes)} dans le profil du compte {account} : "
            f"connecte-toi avec « clipper browser login {account} --url https://www.youtube.com »"
        )
    path = directory / COOKIES_FILE
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(format_netscape(cookies), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass  # Windows : droits herites du dossier
    os.replace(tmp, path)
    return path
