"""Journal global de toutes les actions (TASK-8067) : agrege tout ce qui
passe par ``logging`` dans chaque processus (serve, worker, run et ses
sous-processus, toute commande CLI) dans un fichier par jour, date en heure
de Paris, jamais celle du PC. ``clipper.__main__`` installe le handler dans
chaque processus, independamment de la verbosite console ; clipper.web
journalise les requetes HTTP par-dessus, comme n'importe quel autre logger
(aucune dependance de clipper.web vers clipper.journal pour l'ecriture
elle-meme, seulement pour le resume de corps masque et la lecture de l'API).

Ecriture multi-processus sure sous Windows sans renommage (ADR-ad2e : jamais
de repli silencieux) : sous POSIX, ``os.O_APPEND`` suffit (le noyau garantit
qu'un ``write()`` qui tient dans un seul appel atterrit atomiquement en fin
de fichier). Sous Windows, le mode append du C runtime ne l'est PAS (il fait
un ``lseek`` puis un ``write`` separes) : chaque ecriture prend donc un
verrou ``msvcrt.locking`` sur un octet convenu (offset 0) avant de se placer
en fin de fichier, et le relache juste apres - un mutex inter-processus sans
jamais renommer ni remplacer le fichier. Un echec d'ecriture est journalise
une fois sur stderr, jamais leve (ADR-ad2e).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

if sys.platform == "win32":
    import msvcrt

CONFIG_DEFAULTS: dict[str, object] = {
    "enabled": True,  # journal global desactivable (tests isoles, par exemple)
    "dir": "logs",  # dossier des fichiers journal-AAAA-MM-JJ.log
    "retention_days": 2,  # purge les fichiers dont l'age (en jours) depasse cette valeur
    "level": "INFO",  # niveau minimal journalise, independant de la verbosite console
    "exclude_paths": ["/static/", "/media/"],  # prefixes jamais journalises par le middleware web
}

PARIS = ZoneInfo("Europe/Paris")

_FILE_RE = re.compile(r"^journal-(\d{4})-(\d{2})-(\d{2})\.log$")
_SECRET_KEY_RE = re.compile(r"(password|mot_de_passe|cookie|token|jeton|client_secret|api_key)", re.IGNORECASE)
_MASKED = "***"
_FIELD_SEP = "\t"
_LOCK_BYTE = 1


def _today(tz: ZoneInfo = PARIS) -> date:
    return datetime.now(tz).date()


def _file_for(dir_: Path, day: date) -> Path:
    return dir_ / f"journal-{day.isoformat()}.log"


def purge(dir_: Path, retention_days: int, *, today: date | None = None) -> list[Path]:
    """Supprime les fichiers journal-AAAA-MM-JJ.log de ``dir_`` dont l'age
    (en jours, par rapport a ``today``) depasse ``retention_days`` ; retourne
    les chemins supprimes. N'importe quel autre fichier de ``dir_`` est
    laisse intact."""
    today = today if today is not None else _today()
    removed: list[Path] = []
    if not dir_.is_dir():
        return removed
    for path in dir_.iterdir():
        match = _FILE_RE.match(path.name)
        if match is None:
            continue
        day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if (today - day).days > retention_days:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def mask_secrets(value: Any) -> Any:
    """``value`` (structure JSON) avec les cles dont le nom evoque un mot de
    passe, un cookie ou un jeton masquees (valeur -> '***'), recursivement
    dans les dicts et les listes ; toute autre valeur est rendue telle quelle."""
    if isinstance(value, dict):
        return {
            key: (_MASKED if _SECRET_KEY_RE.search(key) else mask_secrets(sub))
            for key, sub in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    return value


def summarize_body(raw: bytes, *, max_len: int = 500) -> str | None:
    """Resume d'un corps de requete HTTP pour le journal : JSON masque (mots
    de passe/cookies/jetons), tronque a ``max_len`` caracteres ; None si le
    corps est vide. Un corps qui n'est pas du JSON est resume par sa seule
    taille, jamais recopie tel quel (il pourrait contenir un secret qu'aucune
    cle ne permettrait de reperer)."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return f"<corps non-JSON, {len(raw)} octet(s)>"
    text = json.dumps(mask_secrets(data), ensure_ascii=False)
    return text if len(text) <= max_len else text[:max_len] + "…"


def _level(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"[journal] level : niveau de journalisation invalide ({value!r})")
    if isinstance(value, int):
        return value
    resolved = logging.getLevelName(str(value).upper())
    if not isinstance(resolved, int):
        raise ValueError(f"[journal] level : niveau de journalisation inconnu ({value!r})")
    return resolved


def format_line(record: logging.LogRecord, process_tag: str) -> str:
    """Une ligne (toujours terminee par un seul '\\n') : horodatage ISO en
    heure de Paris (jamais celle du PC), tag de processus, niveau, nom du
    logger, message - les retours a la ligne du message sont aplatis pour
    garder une ligne par evenement."""
    when = datetime.fromtimestamp(record.created, PARIS).isoformat()
    message = record.getMessage().replace("\r\n", " ").replace("\n", " ")
    fields = (when, process_tag, record.levelname, record.name, message)
    return _FIELD_SEP.join(fields) + "\n"


def parse_line(raw: str) -> dict[str, Any]:
    """Inverse de ``format_line`` : une ligne malformee (pas 5 champs) revient
    avec tous les champs structures a None et son texte brut dans ``message``
    et ``raw``, jamais rejetee en silence."""
    text = raw.rstrip("\n")
    parts = text.split(_FIELD_SEP, 4)
    if len(parts) != 5:
        return {"timestamp": None, "process": None, "level": None, "logger": None,
                "message": text, "raw": text}
    timestamp, process, level, logger_name, message = parts
    return {"timestamp": timestamp, "process": process, "level": level,
            "logger": logger_name, "message": message, "raw": text}


class JournalHandler(logging.Handler):
    """Un fichier par jour (Europe/Paris) sous ``dir_``, ouvert en append pur
    et jamais renomme. ``logging.Handler.handle`` serialise deja les appels a
    ``emit`` entre threads d'un meme processus (acquire/release autour de
    chaque appel) ; l'exclusion entre PROCESSUS differents est assuree par
    ``_append`` (verrou msvcrt sous Windows, O_APPEND atomique sous POSIX)."""

    def __init__(self, dir_: Path, process_tag: str, retention_days: int) -> None:
        super().__init__()
        self._dir = Path(dir_)
        self._process_tag = process_tag
        self._retention_days = retention_days
        self._fd: int | None = None
        self._day: date | None = None
        self._warned = False
        self._open_for(_today())

    def _open_for(self, day: date) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        purge(self._dir, self._retention_days, today=day)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        self._fd = os.open(str(_file_for(self._dir, day)), flags, 0o644)
        self._day = day

    def _append(self, data: bytes) -> None:
        fd = self._fd
        assert fd is not None
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, _LOCK_BYTE)
            try:
                os.lseek(fd, 0, os.SEEK_END)
                os.write(fd, data)
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_BYTE)
        else:
            os.write(fd, data)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            today = _today()
            # fd ferme : une reconfiguration du logging (uvicorn, dictConfig) ferme tous les handlers
            # existants sans les retirer du logger racine -> on rouvre au lieu de planter la requete.
            if today != self._day or self._fd is None:
                if self._fd is not None:
                    os.close(self._fd)
                self._open_for(today)
            self._append(format_line(record, self._process_tag).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - le journal ne doit jamais casser l'action journalisee
            if not self._warned:
                self._warned = True
                print(f"journal : ecriture impossible ({type(exc).__name__}: {exc})", file=sys.stderr)

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        super().close()


def install(process_kind: str, config: Any) -> JournalHandler | None:
    """Installe (une seule fois par processus) le handler de journal sur le
    logger racine, avec son propre niveau ([journal] level, INFO par defaut)
    independant de celui de la console : si le niveau racine filtre deja plus
    haut (ex. WARNING sans -v), il est abaisse jusqu'a celui du journal pour
    que les evenements lui parviennent quand meme ; les autres handlers
    deja installes (console) ne sont jamais touches ici, c'est a l'appelant
    (clipper.__main__) de fixer leur propre niveau avant cet appel s'il tient
    a garder la console telle quelle. ``enabled = false`` retire le handler
    s'il existait et ne cree ni dossier ni fichier."""
    cfg = config.section("journal")
    root = logging.getLogger()
    existing = next((h for h in root.handlers if isinstance(h, JournalHandler)), None)
    if not cfg["enabled"]:
        if existing is not None:
            root.removeHandler(existing)
            existing.close()
        return None
    if existing is not None:
        return existing
    handler = JournalHandler(Path(cfg["dir"]), f"{process_kind}[{os.getpid()}]", int(cfg["retention_days"]))
    handler.setLevel(_level(cfg["level"]))
    if root.level == logging.NOTSET or root.level > handler.level:
        root.setLevel(handler.level)
    root.addHandler(handler)
    return handler


def install_crash_hooks() -> None:
    """Crochets d'exception du processus : une exception non attrapée (fil principal ou autre fil) est journalisée
    en critique avec sa trace avant de mourir, au lieu de partir sur stderr seulement. Ctrl+C reste à
    l'interpréteur. L'ancien crochet est rappelé ensuite (trace console inchangée)."""
    crash_log = logging.getLogger("clipper.crash")
    previous_sys = sys.excepthook
    previous_thread = threading.excepthook

    def _sys_hook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            crash_log.critical("exception non attrapée : %s", exc, exc_info=(exc_type, exc, tb))
        previous_sys(exc_type, exc, tb)

    def _thread_hook(args):
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread is not None else "?"
            crash_log.critical("exception non attrapée dans le fil %s : %s", name, args.exc_value,
                               exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        previous_thread(args)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


def tail(config: Any, *, limit: int = 200, text: str | None = None, level: str | None = None) -> dict[str, Any]:
    """Dernieres lignes du journal, tous les fichiers encore presents
    (retention_days jours) concatenes dans l'ordre chronologique, filtrees
    puis coupees a ``limit`` : sert GET /api/journal, rien d'autre ne lit ces
    fichiers directement."""
    cfg = config.section("journal")
    dir_ = Path(cfg["dir"])
    if not dir_.is_dir():
        return {"available": False, "path": str(dir_), "lines": [],
                "reason": f"{dir_} introuvable : aucune action journalisee pour l'instant"}
    records: list[dict[str, Any]] = []
    for path in sorted(dir_.glob("journal-*.log")):
        if _FILE_RE.match(path.name) is None:
            continue
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        records.extend(parse_line(line) for line in raw_lines if line)
    if text:
        needle = text.lower()
        records = [r for r in records if needle in r["raw"].lower()]
    if level:
        wanted = level.upper()
        records = [r for r in records if (r["level"] or "").upper() == wanted]
    return {"available": True, "path": str(dir_), "lines": records[-limit:] if limit else records}
