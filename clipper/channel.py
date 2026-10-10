from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from clipper import accounts
from clipper.config import Config, ConfigError, VALID_MODES, load_config, write_config

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_DAYS = accounts.DAYS

CONFIG_DEFAULTS: dict[str, object] = {
    "display_name": "",
    "source_url": "",
    "watch": False,
    "watch_interval_s": 1800,
    "watch_min_duration_s": 600,
    "mode": "",
    "timezone": "Europe/Paris",
    "logo": "",
}

# Cles d'un [channel] d'avant SPEC-6076 R2 : le compte de publication et les creneaux appartiennent au compte
# (clipper.accounts), plus au style. Presentes dans un preset = ignorees (clipper.config les retire de la table),
# avertissement journalise une fois, migration par migrate_legacy_presets, retirees a la sauvegarde du style.
LEGACY_KEYS = ("slots", "tiktok_account")
_warned_legacy: set[str] = set()


class ChannelError(Exception):
    """Invalid channel name, an unknown channel referenced by name, or a
    malformed preset file."""


# Verrou de fichier inter-processus (stdlib seulement) : le worker et l'API
# web sont deux processus (ADR-35b7) qui reecrivent les memes fichiers
# state/. Un cycle lecture-modification-ecriture se fait sous
# ``file_lock(path)``, l'ecriture elle-meme par ``atomic_write_json``.
_REPLACE_ATTEMPTS = 20
_REPLACE_DELAY_S = 0.05
_LOCK_POLL_S = 0.01


@contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    """Verrou exclusif inter-processus sur ``<path>.lock`` (fcntl.flock sous
    Linux/macOS, msvcrt.locking sous Windows), bloquant jusqu'a obtention,
    libere en sortie meme sur exception."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(_LOCK_POLL_S)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def replace_retrying(tmp: Path, path: Path) -> None:
    """``os.replace(tmp, path)``, reessaye tant qu'un lecteur (API web,
    antivirus, indexeur) tient ``path`` ouvert sous Windows (PermissionError).
    Si le verrou persiste, releve l'erreur d'origine (``tmp`` reste a la
    charge de l'appelant) : un etat n'est jamais perdu en silence."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY_S)


def atomic_write_json(path: str | Path, data: Any) -> None:
    """Ecrit ``data`` en JSON dans un fichier temporaire du meme dossier puis
    ``os.replace`` (jamais de fichier a moitie ecrit). Sous Windows, le
    remplacement est reessaye si un lecteur tient encore le fichier ouvert."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        replace_retrying(tmp, path)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise


def _preset_path(presets_dir: str | Path, name: str) -> Path:
    return Path(presets_dir) / f"{name}.toml"


def _validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ChannelError(
            f"nom de chaine invalide : {name!r} (attendu ^[a-z0-9_-]{{1,40}}$)"
        )


def list_channels(presets_dir: str | Path) -> list[str]:
    """Preset names under presets_dir that declare a [channel] table,
    sorted by name. A preset without [channel] stays a valid CLI preset and
    is skipped, its name never checked (SPEC-74e9 1.4)."""
    names = []
    for path in Path(presets_dir).glob("*.toml"):
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ChannelError(f"preset TOML invalide : {path} ({exc})") from exc
        if "channel" in data:
            name = path.stem
            _validate_name(name)
            names.append(name)
    return sorted(names)


def legacy_keys(table: Any) -> list[str]:
    """Les cles d'avant SPEC-6076 R2 presentes dans une table [channel] brute."""
    return [k for k in LEGACY_KEYS if isinstance(table, dict) and k in table]


def _warn_legacy(name: str, keys: list[str]) -> None:
    if keys and name not in _warned_legacy:
        _warned_legacy.add(name)
        logger.warning(
            "style %s : [channel] %s ignoré (le compte de publication et les créneaux sont réglés par compte, "
            "écran Comptes) ; retiré à la prochaine sauvegarde du style", name, " et ".join(keys))


def load_channel(
    name: str,
    *,
    presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
) -> tuple[Config, dict[str, object]]:
    """The merged Config (preset over base) and the validated [channel]
    dict: display_name defaults to the channel name, mode defaults to the
    global config mode (SPEC-74e9 1.3)."""
    _validate_name(name)
    path = _preset_path(presets_dir, name)
    if not path.exists():
        raise ChannelError(f"chaine inconnue : {name!r}")

    config = load_config(path, base=base)
    channel = dict(config.section("channel"))
    with path.open("rb") as f:
        _warn_legacy(name, legacy_keys(tomllib.load(f).get("channel")))

    if not channel["display_name"]:
        channel["display_name"] = name

    if not channel["mode"]:
        channel["mode"] = config.mode
    elif channel["mode"] not in VALID_MODES:
        raise ConfigError(
            f"mode invalide dans [channel] de {name!r}: {channel['mode']!r} "
            f"(attendu: {' | '.join(VALID_MODES)})"
        )

    return config, channel


def save_channel(
    name: str,
    data: dict[str, object],
    *,
    presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
) -> None:
    """Serialize data (the full preset, e.g. {"channel": {...}}) and replace
    the preset file, via config.write_config: reread and validated against
    base first, an invalid file is left intact (SPEC-74e9 1.5). The legacy
    [channel] keys (SPEC-6076 R2) are dropped here, with a log."""
    _validate_name(name)
    table = data.get("channel")
    dropped = legacy_keys(table)
    if dropped:
        data = {**data, "channel": {k: v for k, v in table.items() if k not in LEGACY_KEYS}}
        logger.info("style %s : [channel] %s retiré à la sauvegarde", name, " et ".join(dropped))
    path = _preset_path(presets_dir, name)
    with file_lock(path):
        write_config(path, data, base=base)


def migrate_legacy_presets(
    config: Config,
    *,
    presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
) -> list[str]:
    """Migration unique (SPEC-6076 R2) : pour chaque preset dont [channel] porte encore ``tiktok_account`` et/ou
    ``slots``, les creneaux sont repris sur ce compte (jamais ecrases s'il en a deja ; fuseau du style repris avec
    eux) et ``tiktok_account`` est reporte sur les publications du style qui n'ont encore aucun compte (revue
    r-comptes 11), puis les deux cles sont retirees du fichier. Tout est journalise. Un preset dont le compte est
    inconnu, ou qui a des creneaux sans compte, est laisse tel quel avec un avertissement : rien n'est perdu en
    silence. Rend les noms des presets migres."""
    from clipper import publish as publish_mod  # import tardif : publish importe channel (ADR-b16b)

    migrated = []
    for path in sorted(Path(presets_dir).glob("*.toml")):
        with path.open("rb") as f:
            data = tomllib.load(f)
        table = data.get("channel")
        keys = legacy_keys(table)
        if not keys:
            continue
        name = path.stem
        account_id = table.get("tiktok_account") or ""
        slots = table.get("slots") or []
        if slots:
            if not account_id:
                logger.warning("style %s : créneaux sans compte relié, laissés dans le preset (recopie-les sur un "
                               "compte dans l'écran Comptes, puis sauvegarde le style)", name)
                continue
            try:
                copied = accounts.migrate_slots(config, account_id, slots, table.get("timezone"))
            except accounts.AccountsError as exc:
                logger.warning("style %s : créneaux non migrés vers le compte %s (%s), laissés dans le preset",
                               name, account_id, exc)
                continue
            if copied:
                logger.info("style %s : %d créneau(x) migré(s) sur le compte %s", name, len(slots), account_id)
            else:
                logger.info("style %s : le compte %s a déjà des créneaux, ceux du style ne sont pas repris",
                            name, account_id)
        if account_id:
            moved = publish_mod.migrate_missing_account(
                name, account_id, state_dir=config.section("publish")["state_dir"])
            if moved:
                logger.info("style %s : compte %s reporté sur %d publication(s) sans compte", name, account_id, moved)
        data["channel"] = {k: v for k, v in table.items() if k not in LEGACY_KEYS}
        write_config(path, data, base=base)
        logger.info("style %s : [channel] %s retiré du preset", name, " et ".join(keys))
        migrated.append(name)
    return migrated


def delete_channel(name: str, *, presets_dir: str | Path = "presets") -> None:
    _validate_name(name)
    path = _preset_path(presets_dir, name)
    if not path.exists():
        raise ChannelError(f"chaine inconnue : {name!r}")
    path.unlink()


def next_slots(channel: dict[str, object], after: datetime, n: int) -> list[datetime]:
    """The n next slot datetimes strictly after `after`, in the schedule's
    timezone, chronologically sorted. `channel` is any {"slots", "timezone"}
    dict, in practice accounts.schedule_of(account) (SPEC-6076 R2)."""
    slots = channel["slots"]
    if n <= 0 or not slots:
        return []

    tz = ZoneInfo(str(channel["timezone"]))
    after_local = after.astimezone(tz) if after.tzinfo is not None else after.replace(tzinfo=tz)

    weeks_needed = n // len(slots) + 2
    candidates: list[datetime] = []
    for slot in slots:
        day_idx = _DAYS.index(slot["day"])
        hour, minute = (int(part) for part in slot["time"].split(":"))
        delta_to_day = (day_idx - after_local.weekday()) % 7
        for week in range(weeks_needed):
            candidate = (after_local + timedelta(days=delta_to_day + 7 * week)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate > after_local:
                candidates.append(candidate)

    candidates.sort()
    return candidates[:n]
