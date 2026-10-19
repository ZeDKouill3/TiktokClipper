"""Pays de l'IP publique (TASK-30cc) : affiché dans la console, et garde du navigateur piloté.

Une IP hors du pays attendu ruine la portée d'un compte neuf (deux comptes TikTok publiés depuis une IP UK
n'ont fait aucune vue) : la console l'affiche, et ``clipper.browser`` refuse de piloter hors pays. Service
injoignable = statut « inconnu » (``ok`` None), jamais « ok » par défaut (ADR-ad2e).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from clipper.config import Config

CONFIG_DEFAULTS: dict[str, object] = {
    # Pays attendu de l'IP publique (code ISO 3166-1 alpha-2).
    "expected_country": "FR",
    # Service HTTPS gratuit, sans clé : rend {ip, city, country (ISO), org (AS + FAI)}.
    "geo_url": "https://ipinfo.io/json",
    # Durée (s) pendant laquelle un statut connu est réutilisé.
    "cache_s": 60,
    # Refuse d'ouvrir le navigateur piloté hors du pays attendu (ou pays inconnu).
    "block_browser": True,
}

COUNTRY_NAMES = {
    "FR": "France", "BE": "Belgique", "CH": "Suisse", "LU": "Luxembourg", "CA": "Canada", "GB": "Royaume-Uni",
    "IE": "Irlande", "DE": "Allemagne", "ES": "Espagne", "IT": "Italie", "PT": "Portugal", "NL": "Pays-Bas",
    "US": "États-Unis", "MA": "Maroc", "DZ": "Algérie", "TN": "Tunisie", "SN": "Sénégal", "CI": "Côte d'Ivoire",
}
_TIMEOUT_S = 8

_fetcher: Callable[[str], dict[str, Any]] | None = None  # remplacé par les tests : aucun réseau
_clock: Callable[[], float] | None = None
_lock = threading.Lock()
_cache: dict[str, Any] = {}  # {"url", "at", "geo"} : seul un relevé connu est gardé


class NetworkError(Exception):
    """IP hors du pays attendu alors que le navigateur piloté est bloqué (arrêt sûr)."""


class NetworkUnknown(NetworkError):
    """Pays de l'IP inconnu (service de géolocalisation injoignable) : condition transitoire, pas un arrêt sûr."""


def use_fetcher(fetcher: Callable[[str], dict[str, Any]] | None) -> None:
    """Branche la lecture du service de géolocalisation (tests : simulée) ; None = le vrai, par HTTPS."""
    global _fetcher
    _fetcher = fetcher


def use_clock(clock: Callable[[], float] | None) -> None:
    global _clock
    _clock = clock


def reset() -> None:
    with _lock:
        _cache.clear()


def _settings(config: Config | None) -> dict[str, object]:
    return config.section("network") if config is not None else dict(CONFIG_DEFAULTS)


def _fetch(url: str) -> dict[str, Any]:
    if _fetcher is not None:
        return _fetcher(url)
    if not url.startswith("https://"):
        raise ValueError(f"geo_url doit être en HTTPS : {url}")
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "clipper"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310 - HTTPS vérifié ci-dessus
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("réponse du service de géolocalisation illisible")
    return data


def _geo(url: str, cache_s: float) -> dict[str, Any]:
    """Relevé brut {ip, city, country, org} ; lève si le service ne répond pas ou sans pays."""
    now = (_clock or time.monotonic)()
    with _lock:
        if _cache.get("url") == url and now - _cache["at"] < cache_s:
            return _cache["geo"]
    data = _fetch(url)
    country = data.get("country")
    if not isinstance(country, str) or len(country.strip()) != 2:
        raise ValueError("pas de champ country dans la réponse du service")
    geo = {
        "ip": data.get("ip"), "city": data.get("city"), "isp": data.get("org") or data.get("isp"),
        "country": country.strip().upper(),
    }
    with _lock:
        _cache.update({"url": url, "at": now, "geo": geo})
    return geo


def status(config: Config | None = None, expected: str | None = None) -> dict[str, Any]:
    """{ip, country, country_name, city, isp, ok, expected_country, checked_at, error}. ``ok`` : True (pays
    attendu), False (autre pays) ou None (inconnu : service injoignable ou réponse sans pays)."""
    settings = _settings(config)
    expected = (expected or "").strip().upper() or str(settings["expected_country"]).strip().upper()
    base = {"expected_country": expected, "expected_country_name": COUNTRY_NAMES.get(expected, expected),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "block_browser": bool(settings["block_browser"])}
    try:
        geo = _geo(str(settings["geo_url"]), float(settings["cache_s"]))
    except Exception as exc:  # noqa: BLE001 - réseau, JSON, pays absent : tout devient « inconnu », rien d'inventé
        return {**base, "ip": None, "country": None, "country_name": None, "city": None, "isp": None, "ok": None,
                "error": str(exc) or type(exc).__name__}
    country = geo["country"]
    return {**base, **geo, "country_name": COUNTRY_NAMES.get(country, country), "ok": country == expected, "error": None}


def require_expected_country(config: Config | None = None, *, expected: str | None = None,
                             account: str | None = None) -> None:
    """Pays attendu : ``expected`` (celui du compte piloté) s'il est renseigné, sinon ``[network]``;
    ``account`` nomme le compte dans le message. Garde du navigateur piloté : ``NetworkError`` si ``block_browser`` et pays inconnu ou différent."""
    if not _settings(config)["block_browser"]:
        return
    state = status(config, expected)
    if state["ok"]:
        return
    who = f"compte {account} : " if account else ""
    advice = "passe sur le partage de connexion du téléphone"
    if state["ok"] is None:
        raise NetworkUnknown(
            f"{who}pays de l'IP inconnu ({state['error']}), attendu {state['expected_country_name']} : {advice}"
        )
    raise NetworkError(f"{who}IP en {state['country_name']} (attendu {state['expected_country_name']}) : {advice}")
