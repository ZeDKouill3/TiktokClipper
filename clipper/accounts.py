"""Carnet local des comptes (SPEC-6fa4) : libelle, plateforme, identifiant,
notes dans state/accounts.json ; le mot de passe va UNIQUEMENT dans le coffre
de l'OS via keyring (service « clipper-accounts », cle = id du compte).

Pas de repli (ADR-ad2e) : sans coffre sur (backend fail/null/en clair), toute
operation qui touche un mot de passe echoue avec une erreur explicite, jamais
vers un fichier, une variable ou la memoire. Aucun message d'erreur, aucune
trace ni aucun journal ne porte un mot de passe : les exceptions de keyring
sont remplacees par un message maison, jamais recopiees.

Compte de publication (SPEC-e500 R3) : en plus, l'etat de connexion TikTok verifie par l'appelant
(``login`` : never | connected | expired, jamais lu ici : aucun import d'une autre etape), l'arret R4
en attente (``r4_halt``) et la case « pret a publier », qui n'est PAS un choix : elle est calculee
(connexion verifiee ET aucun arret R4 en attente), cochee et decochee d'elle-meme avec journal +
``ready_note`` ; un arret R4 ne s'efface que par ``clear_halt`` (« J'ai regle le probleme »).

Service (SPEC-5e50 R1) : chaque compte de publication porte un ``service`` (``tiktok`` | ``youtube``) ; les
comptes existants, sans ce champ, sont lus ``tiktok`` (migration a la lecture, ecrite au prochain
enregistrement). Un compte YouTube est « pret » quand une verification a vu YouTube Studio ouvert sur une
chaine (``channel`` : nom + identifiant UC...), calculee par l'appelant : ce module ne navigue pas.

Module d'etape isole : n'importe aucune autre etape (ADR-b16b) ; la route web
passe par ici, le generateur de mot de passe aussi (secrets, CSPRNG).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import string
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import keyring
from keyring.errors import PasswordDeleteError

from clipper.config import Config

logger = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, object] = {
    # Fichier des comptes (sans mot de passe) ; state/ est ignore par git.
    "state_file": "state/accounts.json",
    # Longueur par defaut du mot de passe genere (bornes : 12 a 64).
    "password_length": 20,
}

SERVICE = "clipper-accounts"
MIN_LENGTH = 12
MAX_LENGTH = 64
_AMBIGUOUS = "Il1O0o|"
_SYMBOLS = "!@#$%^&*()-_=+[]{};:,.?"
_FIELDS = ("label", "platform", "username", "notes")
_MAX_LEN = {"label": 120, "platform": 60, "username": 254, "notes": 2000}
_MAX_PASSWORD = 512
LOGIN_STATES = ("never", "connected", "expired")
SERVICES = ("tiktok", "youtube")
DEFAULT_SERVICE = "tiktok"
SERVICE_LABELS = {"tiktok": "TikTok", "youtube": "YouTube"}
_REPLACE_ATTEMPTS = 5
_REPLACE_DELAY_S = 0.05
# Creneaux reguliers d'un compte (SPEC-6076 R2) : jour + heure locale, dans le fuseau du compte.
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_TIMEZONE = "Europe/Paris"
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_lock = threading.Lock()
_backend_override: Any = None


class AccountsError(Exception):
    """Entree invalide, coffre indisponible ou fichier illisible."""


class AccountNotFound(AccountsError):
    """Compte (ou mot de passe) inexistant."""


class VaultUnavailable(AccountsError):
    """Pas de coffre sur : on refuse, sans repli (R2)."""


def use_backend(backend: Any) -> None:
    """Branche un backend keyring (tests : en memoire) ; None = celui de l'OS."""
    global _backend_override
    _backend_override = backend


# ---------------------------------------------------------------- coffre


def _backend_is_unsafe(backend: Any) -> bool:
    members = getattr(backend, "backends", None)
    if members is not None:  # ChainerBackend : tous ses membres doivent etre surs
        members = list(members)
        return not members or any(_backend_is_unsafe(b) for b in members)
    cls = type(backend)
    if cls.__module__.startswith(("keyring.backends.fail", "keyring.backends.null", "keyrings.alt")):
        return True
    if "plaintext" in cls.__name__.lower():
        return True
    try:
        return float(backend.priority) <= 0
    except Exception:  # noqa: BLE001 - priority leve RuntimeError sur un backend inutilisable
        return True


def _vault() -> Any:
    try:
        backend = _backend_override if _backend_override is not None else keyring.get_keyring()
    except Exception as exc:  # noqa: BLE001
        logger.error("coffre : initialisation impossible (%s)", type(exc).__name__)
        raise VaultUnavailable("coffre de l'OS indisponible : keyring n'a pas pu s'initialiser") from None
    if _backend_is_unsafe(backend):
        raise VaultUnavailable(
            "pas de coffre sûr sur ce poste (keyring n'a trouvé ni Gestionnaire d'identification "
            "Windows, ni trousseau, ni Secret Service) : le mot de passe n'est pas enregistré, "
            "il n'est jamais écrit dans un fichier ni gardé en mémoire"
        )
    return backend


def _vault_call(action: str, fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - jamais str(exc) : il pourrait porter le mot de passe
        logger.error("coffre : %s impossible (%s)", action, type(exc).__name__)
        raise AccountsError(f"le coffre de l'OS a refusé l'opération : {action}") from None


def _vault_set(account_id: str, password: str) -> None:
    backend = _vault()
    _vault_call("écriture du mot de passe", backend.set_password, SERVICE, account_id, password)


def _vault_get(account_id: str) -> str | None:
    backend = _vault()
    return _vault_call("lecture du mot de passe", backend.get_password, SERVICE, account_id)


def _vault_delete(account_id: str) -> None:
    backend = _vault()
    try:
        backend.delete_password(SERVICE, account_id)
    except PasswordDeleteError:
        if _vault_call("lecture du mot de passe", backend.get_password, SERVICE, account_id) is not None:
            raise AccountsError("le coffre de l'OS a refusé l'opération : suppression du mot de passe") from None
    except Exception as exc:  # noqa: BLE001
        logger.error("coffre : suppression du mot de passe impossible (%s)", type(exc).__name__)
        raise AccountsError("le coffre de l'OS a refusé l'opération : suppression du mot de passe") from None


# ---------------------------------------------------------------- creneaux


def validate_slots(slots: Any) -> list[dict[str, str]]:
    """Creneaux reguliers ``[{"day": "mon".."sun", "time": "HH:MM"}]`` : liste validee et dedoublonnee, erreur
    explicite sinon (jamais un creneau ignore)."""
    if not isinstance(slots, list):
        raise AccountsError("slots : une liste de créneaux {\"day\", \"time\"} est attendue")
    out: list[dict[str, str]] = []
    for slot in slots:
        day = slot.get("day") if isinstance(slot, dict) else None
        time_str = slot.get("time") if isinstance(slot, dict) else None
        if day not in DAYS or not isinstance(time_str, str) or not _TIME_RE.match(time_str):
            raise AccountsError(
                f"créneau invalide : {slot!r} (attendu : jour {' | '.join(DAYS)} et heure HH:MM)")
        if isinstance(slot, dict) and set(slot) - {"day", "time"}:
            raise AccountsError(f"créneau invalide : {slot!r} (seuls day et time sont admis)")
        if {"day": day, "time": time_str} not in out:
            out.append({"day": day, "time": time_str})
    return out


def validate_timezone(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise AccountsError("timezone : un nom de fuseau horaire est attendu (ex. Europe/Paris)")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise AccountsError(f"timezone : fuseau horaire inconnu ({name!r})") from None
    return name


def validate_expected_country(value: Any) -> str:
    """Pays attendu de l'IP d'un compte : code ISO a deux lettres (majuscules), "" = celui de [network]."""
    if not isinstance(value, str) or (value.strip() and not re.fullmatch(r"[A-Za-z]{2}", value.strip())):
        raise AccountsError(f"expected_country : code pays ISO à deux lettres attendu (GB, FR...), reçu {value!r}")
    return value.strip().upper()


def country_guard(config: Config, account_id: str) -> tuple[str | None, str | None]:
    """``(pays attendu propre au compte ou None, libellé du compte ou None)`` : None = compte inconnu ou champ vide
    (le garde réseau retombe alors sur [network] expected_country)."""
    with _lock:
        account = next((a for a in _read(config) if a.get("id") == account_id), None)
    if account is None:
        return None, None
    return account.get("expected_country") or None, account.get("label") or account_id


def expected_country_of(config: Config, account_id: str) -> str | None:
    return country_guard(config, account_id)[0]


def schedule_of(account: dict[str, Any]) -> dict[str, Any]:
    """Les creneaux d'un compte sous la forme que lit ``clipper.channel.next_slots`` : ``{"slots", "timezone"}``."""
    return {"slots": list(account.get("slots") or []), "timezone": account.get("timezone") or DEFAULT_TIMEZONE}


def migrate_slots(config: Config, account_id: str, slots: list[dict[str, str]], timezone_name: str | None = None) -> bool:
    """Reprend sur le compte les creneaux d'un ancien style (SPEC-6076 R2), une seule fois : rend False, sans rien
    ecrire, si le compte a deja des creneaux (jamais ecrases) ; ``AccountNotFound`` si le compte n'existe pas."""
    slots = validate_slots(slots)
    with _lock, _file_lock(config):
        accounts = _read(config)
        account = _find(accounts, account_id)
        if account.get("slots") or not slots:
            return False
        account["slots"] = slots
        if timezone_name:
            account["timezone"] = validate_timezone(timezone_name)
        account["updated_at"] = _now()
        _write(config, accounts)
    logger.info("compte %s : %d créneau(x) repris d'un style", account_id, len(slots))
    return True


# ---------------------------------------------------------------- fichier


def _path(config: Config) -> Path:
    return Path(str(config.section("accounts")["state_file"]))


def _read(config: Config) -> list[dict[str, Any]]:
    path = _path(config)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AccountsError(f"fichier des comptes illisible : {path} ({type(exc).__name__})") from None
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise AccountsError(f"fichier des comptes invalide : {path} (attendu : {{\"accounts\": [...]}})")
    for account in data["accounts"]:
        if isinstance(account, dict):
            account.setdefault("service", DEFAULT_SERVICE)  # migration : comptes d'avant YouTube = tiktok
    return data["accounts"]


def _write(config: Config, accounts: list[dict[str, Any]]) -> None:
    path = _path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"accounts": accounts}, f, ensure_ascii=False, indent=2)
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows : fichier momentanement verrouille
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_DELAY_S)
    except OSError as exc:
        Path(tmp).unlink(missing_ok=True)
        raise AccountsError(f"écriture du fichier des comptes impossible : {path} ({type(exc).__name__})") from None


def _file_lock(config: Config):
    """Verrou inter-processus sur le fichier des comptes, en plus du verrou de thread (ADR-35b7, revue r-comptes
    3) : import tardif pour eviter le cycle accounts <-> channel (channel importe accounts pour DAYS)."""
    from clipper import channel as channel_mod

    return channel_mod.file_lock(_path(config))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _public(account: dict[str, Any]) -> dict[str, Any]:
    out = {k: account.get(k, "") for k in ("id", *_FIELDS)}
    out["service"] = account.get("service") or DEFAULT_SERVICE
    out.update(has_password=bool(account.get("has_password")), channel=account.get("channel"),
               created_at=account.get("created_at"), updated_at=account.get("updated_at"),
               ready_to_publish=bool(account.get("ready_to_publish")),
               ready_note=account.get("ready_note"), paused_at=account.get("paused_at"), login=account.get("login"),
               r4_halt=account.get("r4_halt"), slots=list(account.get("slots") or []),
               timezone=account.get("timezone") or DEFAULT_TIMEZONE,
               expected_country=account.get("expected_country") or "")
    return out


def _clean(data: Any, *, creating: bool) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AccountsError("corps invalide : un objet JSON est attendu")
    unknown = set(data) - {*_FIELDS, "password", "service", "slots", "timezone", "expected_country"}
    if unknown:
        raise AccountsError(f"champ(s) inconnu(s) : {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for key in _FIELDS:
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str):
            raise AccountsError(f"{key} : une chaîne est attendue")
        value = value.strip()
        if len(value) > _MAX_LEN[key]:
            raise AccountsError(f"{key} : {_MAX_LEN[key]} caractères au maximum")
        out[key] = value
    if creating and not out.get("label"):
        raise AccountsError("label : un libellé est obligatoire")
    if not creating and "label" in out and not out["label"]:
        raise AccountsError("label : un libellé est obligatoire")
    if "service" in data:
        if data["service"] not in SERVICES:
            raise AccountsError(f"service : {data['service']!r} invalide (attendu : {' | '.join(SERVICES)})")
        out["service"] = data["service"]
    if "slots" in data:
        out["slots"] = validate_slots(data["slots"])
    if "timezone" in data:
        out["timezone"] = validate_timezone(data["timezone"])
    if "expected_country" in data:
        out["expected_country"] = validate_expected_country(data["expected_country"])
    if "password" in data:
        password = data["password"]
        if not isinstance(password, str):
            raise AccountsError("password : une chaîne est attendue")
        if len(password) > _MAX_PASSWORD:
            raise AccountsError(f"password : {_MAX_PASSWORD} caractères au maximum")
        out["password"] = password
    return out


# ---------------------------------------------------------------- comptes


def list_accounts(config: Config) -> list[dict[str, Any]]:
    """Comptes sans mot de passe (champ has_password seulement, R4)."""
    with _lock:
        return [_public(a) for a in _read(config)]


def _find(accounts: list[dict[str, Any]], account_id: str) -> dict[str, Any]:
    for account in accounts:
        if account.get("id") == account_id:
            return account
    raise AccountNotFound(f"compte introuvable : {account_id!r}")


def add_account(config: Config, data: Any) -> dict[str, Any]:
    fields = _clean(data, creating=True)
    password = fields.pop("password", "")
    with _lock, _file_lock(config):
        accounts = _read(config)
        account_id = secrets.token_hex(6)
        if password:
            _vault_set(account_id, password)
        now = _now()
        account = {"id": account_id, **{k: fields.get(k, "") for k in _FIELDS},
                   "service": fields.get("service", DEFAULT_SERVICE), "has_password": bool(password), "created_at": now, "updated_at": now,
                   "slots": fields.get("slots", []), "timezone": fields.get("timezone", DEFAULT_TIMEZONE),
                   "expected_country": fields.get("expected_country", "")}
        try:
            _write(config, [*accounts, account])
        except AccountsError:
            if password:
                _vault_delete(account_id)  # pas de secret orphelin dans le coffre
            raise
    logger.info("compte ajouté : %s", account_id)
    return _public(account)


def update_account(config: Config, account_id: str, data: Any) -> dict[str, Any]:
    """Champs absents inchangés ; password absent = inchangé, "" = supprimé du coffre."""
    fields = _clean(data, creating=False)
    password = fields.pop("password", None)
    with _lock, _file_lock(config):
        accounts = _read(config)
        account = _find(accounts, account_id)
        if "service" in fields and fields["service"] != account.get("service"):
            raise AccountsError("service : le service d'un compte ne change pas (supprime-le et recrée-le)")
        if password:
            _vault_set(account_id, password)
            account["has_password"] = True
        elif password == "" and account.get("has_password"):
            _vault_delete(account_id)
            account["has_password"] = False
        account.update(fields)
        account["updated_at"] = _now()
        _write(config, accounts)
    logger.info("compte modifié : %s", account_id)
    return _public(account)


def delete_account(config: Config, account_id: str) -> None:
    """Supprime le compte ET son entrée du coffre (le coffre d'abord : pas de secret orphelin)."""
    with _lock, _file_lock(config):
        accounts = _read(config)
        account = _find(accounts, account_id)
        if account.get("has_password"):
            _vault_delete(account_id)
        _write(config, [a for a in accounts if a is not account])
    logger.info("compte supprimé : %s", account_id)


def get_password(config: Config, account_id: str) -> str:
    """Seule sortie d'un mot de passe : appelée au clic (Afficher / Copier)."""
    with _lock:
        account = _find(_read(config), account_id)
    if not account.get("has_password"):
        raise AccountNotFound(f"aucun mot de passe enregistré pour le compte {account_id!r}")
    password = _vault_get(account_id)
    if password is None:
        raise AccountsError(
            f"le coffre de l'OS ne contient plus le mot de passe du compte {account_id!r} : "
            "saisis-le de nouveau (Modifier)"
        )
    return password


# ---------------------------------------------------------------- compte de publication


def _paris(iso: str) -> str:
    """Date affichee en heure de Paris (SPEC-5e50 R8)."""
    try:
        return datetime.fromisoformat(iso).astimezone(ZoneInfo(DEFAULT_TIMEZONE)).strftime("%d/%m/%Y à %H:%M")
    except ValueError:
        return iso


def pause_reason(account: dict[str, Any]) -> str | None:
    """Raison de la pause manuelle (SPEC-f348 R3 c), None si le compte n'est pas en pause."""
    if not account.get("paused_at"):
        return None
    return f"En pause (manuel) depuis le {paused_since(account)} : recoche « Prêt à publier » pour reprendre"


def paused_since(account: dict[str, Any]) -> str | None:
    """Date de la pause manuelle en heure de Paris, None si le compte n'est pas en pause."""
    return _paris(account["paused_at"]) if account.get("paused_at") else None


def ready_blocked_reason(account: dict[str, Any]) -> str | None:
    """Pourquoi le compte n'est pas « pret a publier » (None : il l'est) : pause manuelle (R3 c), puis connexion
    du service non verifiee ou expiree, ou arret R4 en attente (R3)."""
    return pause_reason(account) or connection_blocked_reason(account)


def connection_blocked_reason(account: dict[str, Any]) -> str | None:
    """La part connexion / arret R4 de ``ready_blocked_reason``, sans la pause : un compte en pause connecte et sans
    arret reste relevable pour les statistiques (SPEC-f348 R7.5)."""
    service = account.get("service") or DEFAULT_SERVICE
    label = SERVICE_LABELS.get(service, service)
    login = account.get("login") or {}
    if login.get("state") == "expired":
        return f"session {label} expirée : reconnecte-toi (Se connecter)"
    if login.get("state") != "connected":
        if login.get("reason"):
            return f"connexion {label} non vérifiée : {login['reason']}"
        if service == "youtube":
            return ("connexion YouTube non vérifiée : clique sur Se connecter, connecte-toi à la main à "
                    "YouTube Studio puis ferme la fenêtre")
        return "connexion TikTok non vérifiée : clique sur Se connecter, connecte-toi à la main puis ferme la fenêtre"
    halt = account.get("r4_halt")
    if halt:
        return (f"arrêt de publication en attente ({halt.get('reason') or 'raison inconnue'}) : "
                "règle le problème puis clique sur « J'ai réglé le problème »")
    return None


def _sync_ready(account: dict[str, Any], account_id: str) -> str | None:
    """Recalcule ``ready_to_publish`` depuis ``login`` et ``r4_halt`` (R3), journalise chaque changement avec sa
    raison. Rend ``"checked"``, ``"unchecked"`` ou None (inchange). L'appelant ecrit le fichier."""
    reason = ready_blocked_reason(account)
    if reason is None and not account.get("ready_to_publish"):
        account.update(ready_to_publish=True, ready_note=None)
        logger.info("compte %s : « prêt à publier » coché automatiquement : connexion %s vérifiée, "
                    "aucun arrêt en attente", account_id, SERVICE_LABELS.get(account.get("service"), "TikTok"))
        return "checked"
    if reason is not None and account.get("ready_to_publish"):
        account.update(ready_to_publish=False, ready_note=f"décoché automatiquement le {_now()} : {reason}")
        logger.warning("compte %s : « prêt à publier » décoché (%s)", account_id, reason)
        return "unchecked"
    return None


def _login_changed(previous: dict[str, Any] | None, login: dict[str, Any]) -> bool:
    """Compare l'etat de connexion observe sans ``checked_at`` : un simple rafraichissement de l'horodatage
    n'est pas un changement a ecrire (revue r-comptes 3)."""
    if previous is None:
        return True
    keys = (set(previous) | set(login)) - {"checked_at"}
    return any(previous.get(k) != login.get(k) for k in keys)


def record_login(config: Config, account_id: str, observed: dict[str, Any]) -> dict[str, Any]:
    """Enregistre la connexion verifiee (``{"state", "checked_at", "expires_at"}``, R2) et recalcule la case
    « pret a publier » (R3) : cochee quand la connexion est verifiee et qu'aucun arret R4 n'attend, decochee
    sinon (journal, ``ready_note``). Un cookie de session disparu d'un compte qui etait connecte vaut
    « expired ». La reponse porte ``auto_checked`` / ``auto_unchecked`` (l'appelant fait du decochage un
    evenement console)."""
    state = observed.get("state") if isinstance(observed, dict) else None
    if state not in LOGIN_STATES:
        raise AccountsError(f"état de connexion invalide : {state!r} (attendu : {' | '.join(LOGIN_STATES)})")
    channel = observed.get("channel")
    if channel is not None and not (isinstance(channel, dict) and all(
            isinstance(channel.get(k), str) and channel[k] for k in ("name", "id"))):
        raise AccountsError("chaîne invalide : {\"name\": nom, \"id\": identifiant} attendu")
    with _lock, _file_lock(config):
        accounts = _read(config)
        account = _find(accounts, account_id)
        if account.get("service") == "youtube" and state == "connected" and channel is None:
            raise AccountsError("connexion YouTube sans chaîne : le nom et l'identifiant de la chaîne sont requis")
        previous_login = account.get("login")
        previous = (previous_login or {}).get("state")
        if state == "never" and previous in ("connected", "expired"):
            state = "expired"  # Chrome a purge le cookie expire : le compte etait connecte
        login = {"state": state, "checked_at": observed.get("checked_at") or _now(),
                 "expires_at": observed.get("expires_at")}
        if observed.get("reason"):
            login["reason"] = str(observed["reason"])
        changed = _login_changed(previous_login, login)
        account["login"] = login
        if channel is not None:
            previous_channel = account.get("channel")
            account["channel"] = {"name": channel["name"], "id": channel["id"]}
            changed = changed or account["channel"] != previous_channel
        change = _sync_ready(account, account_id)
        if changed or change:
            _write(config, accounts)
        out = _public(account)
    out.update(auto_checked=change == "checked", auto_unchecked=change == "unchecked")
    return out


def uncheck_ready(config: Config, account_id: str, reason: str) -> bool:
    """Enregistre un arret R4 (captcha, verification...) : la case se decoche et reste decochee, avec la
    raison affichee, tant que ``clear_halt`` n'a pas ete appele ; rend True si la case etait cochee."""
    with _lock, _file_lock(config):
        accounts = _read(config)
        account = _find(accounts, account_id)
        was_ready = bool(account.get("ready_to_publish"))
        if not account.get("r4_halt"):  # un arret deja en attente garde sa raison d'origine
            account["r4_halt"] = {"reason": reason, "at": _now()}
        account.update(ready_to_publish=False,
                       ready_note=f"décoché automatiquement le {_now()} : {reason}" if was_ready else account.get("ready_note"))
        _write(config, accounts)
    logger.warning("compte %s : arrêt de publication enregistré (%s)%s", account_id, reason,
                   ", « prêt à publier » décoché" if was_ready else "")
    return was_ready


def clear_halt(config: Config, account_id: str) -> dict[str, Any]:
    """« J'ai regle le probleme » (R3) : efface l'arret R4 en attente ; la case suit alors la derniere connexion
    verifiee (l'appelant la revérifie juste avant). Journalise l'effacement."""
    with _lock, _file_lock(config):
        accounts = _read(config)
        account = _find(accounts, account_id)
        halt = account.pop("r4_halt", None)
        change = _sync_ready(account, account_id)
        if halt or change:
            account["updated_at"] = _now()
            _write(config, accounts)
        out = _public(account)
    if halt:
        logger.info("compte %s : arrêt de publication effacé par l'utilisateur (%s)", account_id, halt.get("reason"))
    out.update(auto_checked=change == "checked", auto_unchecked=change == "unchecked")
    return out


def pause(config: Config, account_id: str) -> dict[str, Any]:
    """Pause manuelle (SPEC-f348 R3 c, R7.1) : pose ``paused_at`` (ISO UTC), decoche la case avec la raison
    « En pause (manuel) depuis le <date> ». Idempotent. La connexion et l'arret R4 restent enregistres tels quels."""
    with _lock, _file_lock(config):
        accounts = _read(config)
        account = _find(accounts, account_id)
        changed = not account.get("paused_at")
        was_ready = bool(account.get("ready_to_publish"))
        if changed:
            account["paused_at"] = _now()
            account["ready_to_publish"] = False
            account["ready_note"] = pause_reason(account)
            account["updated_at"] = _now()
            _write(config, accounts)
        out = _public(account)
    if changed:
        logger.warning("compte %s : mis en pause manuellement%s", account_id,
                       ", « prêt à publier » décoché" if was_ready else "")
    out.update(auto_checked=False, auto_unchecked=changed and was_ready)
    return out


def resume(config: Config, account_id: str) -> dict[str, Any]:
    """Reprise (SPEC-f348 R7.2) : retire ``paused_at`` ; la case suit R3 (cochee si connexion verifiee et aucun arret
    R4, sinon decochee avec la raison de connexion ou d'arret). Sans pause : rien ne change."""
    with _lock, _file_lock(config):
        accounts = _read(config)
        account = _find(accounts, account_id)
        was_paused = bool(account.pop("paused_at", None))
        change = None
        if was_paused:
            change = _sync_ready(account, account_id)
            if not account.get("ready_to_publish"):
                account["ready_note"] = connection_blocked_reason(account)
            account["updated_at"] = _now()
            _write(config, accounts)
        out = _public(account)
    if was_paused:
        logger.info("compte %s : pause manuelle levée (reprise)%s", account_id,
                    ", « prêt à publier » coché" if change == "checked" else "")
    out.update(auto_checked=change == "checked", auto_unchecked=False)
    return out


# ---------------------------------------------------------------- generateur


def generate_password(
    config: Config, length: Any = None, *, symbols: bool = False, avoid_ambiguous: bool = False
) -> str:
    """Mot de passe aleatoire (secrets) : minuscules, majuscules, chiffres, symboles en option,
    au moins un caractere de chaque classe choisie."""
    if length is None:
        length = config.section("accounts")["password_length"]
    if isinstance(length, bool) or not isinstance(length, int):
        raise AccountsError("longueur invalide : un entier est attendu")
    if not MIN_LENGTH <= length <= MAX_LENGTH:
        raise AccountsError(f"longueur invalide : {length} (attendu : {MIN_LENGTH} à {MAX_LENGTH})")
    classes = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
    if symbols:
        classes.append(_SYMBOLS)
    if avoid_ambiguous:
        classes = ["".join(c for c in cls if c not in _AMBIGUOUS) for cls in classes]
    chars = [secrets.choice(cls) for cls in classes]
    pool = "".join(classes)
    chars += [secrets.choice(pool) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
