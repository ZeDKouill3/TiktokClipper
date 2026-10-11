"""Écran Veille v3 (TASK-3c87a337ffc2, ADR-a308, SPEC-7715) : sommaire, sources en table, jaquettes bornées.

Aucun réseau ni navigateur : veille.js est évalué entier sous node (vm, bouchons de page) et on lit le HTML rendu.
Fixtures sans nom réel.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "clipper" / "web" / "static"
NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")

_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [src, input] = process.argv.slice(2);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ctx = { console, document: { addEventListener() {} }, esc, CLIPPER_TZ: "Europe/Paris", Screens: {}, store: {},
  fr: (n, d) => Number(n).toLocaleString("fr-FR", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 }),
  icon: (name) => `<svg data-icon="${name}"></svg>`, currentScreen: "veille", renderCurrent() {}, updateCounts() {},
  api: async () => ({}), emptyState: (i, t) => `<div>${t}</div>` };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(src, "utf8"), ctx);
const jobs = JSON.parse(fs.readFileSync(input, "utf8"));
process.stdout.write(JSON.stringify(jobs.map((expr) => vm.runInContext(expr, ctx))));
"""


def _eval(tmp_path: Path, data: dict, *exprs: str) -> list:
    """Chaque expression JS est évaluée dans veille.js chargé entier, `D` valant `data` (et veilleUi.data)."""
    (tmp_path / "h.js").write_text(_HARNESS, encoding="utf-8")
    prelude = f"var D = {json.dumps(data)}; veilleUi.data = D; 0"
    (tmp_path / "jobs.json").write_text(json.dumps([prelude, *exprs]), encoding="utf-8")
    done = subprocess.run(["node", str(tmp_path / "h.js"), str(STATIC / "screens" / "veille.js"),
                           str(tmp_path / "jobs.json")], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)[1:]


def _entry(n: int, days: int) -> dict:
    when = (datetime(2026, 10, 7) + timedelta(days=days)).date().isoformat()
    return {"igdb_id": n, "name": f"Jeu {n}", "key": f"jeu {n}", "slug": f"jeu-{n}", "url": None, "hypes": 10 + n,
            "cover_image_id": f"co{n}", "steam_appid": None, "first_release_date": None, "date": when, "human": when,
            "days": days, "platforms": ["PC"], "regions": [], "statuses": [], "portage": False, "trend": None}


def _data(recent=(), upcoming=(), sources=None) -> dict:
    ok = {"status": "ok", "at": "x", "error": None, "counts": {}}
    return {"enabled": True, "running": False, "next_run_at": None,
            "settings": {"upcoming_days": 14, "run_at": "07:00", "igdb_min_hypes": 0},
            "selection": None,
            "day": {"date": "2026-10-07", "started_at": "2026-10-07T05:00:00+00:00",
                    "finished_at": "2026-10-07T05:01:00+00:00",
                    "sources": sources if sources is not None else {"igdb": ok, "twitch": ok},
                    "llm": {"status": "ok", "error": None}, "games": [], "proposals": [], "excluded": {},
                    "releases": {"recent": list(recent), "upcoming": list(upcoming), "excluded_low_hypes": 0,
                                 "truncated": {"recent": 0, "upcoming": 0}}}}


def _covers(html: str) -> int:
    return len(re.findall(r'<span class="cover[ "]', html))


def _between(html: str, start: str, end: str | None = None) -> str:
    begin = html.index(start)
    return html[begin:html.index(end, begin)] if end else html[begin:]


# --- sommaire : ancres des 5 sections ---------------------------------------------------------------------------

SECTION_IDS = ("veille-releve", "veille-propositions", "veille-sorties", "veille-montent", "veille-clips")
VIEW = "(function(){ const b = { innerHTML: '', childElementCount: 0 }; veilleView(b); return b.innerHTML; })()"


@NODE
def test_toc_links_the_five_sections_and_each_anchor_exists_in_the_rendered_view(tmp_path):
    toc, view = _eval(tmp_path, _data(), "veilleToc(D)", VIEW)
    assert re.findall(r'href="#([a-z-]+)"', toc) == list(SECTION_IDS)
    for anchor in SECTION_IDS:
        assert f'id="{anchor}"' in view, anchor
    assert view.index("data-veille-toc") < view.index('id="veille-releve"')  # le sommaire est en tête


# --- sources : une ligne par source, avec état et erreur --------------------------------------------------------

@NODE
def test_sources_are_a_table_with_one_row_per_source_with_state_counts_and_error(tmp_path):
    sources = {
        "twitch": {"status": "ok", "at": "x", "error": None, "counts": {"vods": 4, "restricted": 2}},
        "youtube": {"status": "error", "at": "x", "error": "clé API refusée", "counts": {}},
        "steam": {"status": "partial", "at": "x", "error": "échéance atteinte", "counts": {"rows": 3}},
    }
    (html,) = _eval(tmp_path, _data(sources=sources), "veilleSources(D)")
    assert "<table" in html
    rows = dict(re.findall(r'<tr data-veille-source="([a-z_0-9]+)">(.*?)</tr>', html, re.S))
    assert len(rows) == 9  # une ligne par source connue, relevée ou non
    assert "4 VOD" in rows["twitch"] and "2 VOD abonnés écartées" in rows["twitch"] and "erreur" not in rows["twitch"]
    assert "erreur" in rows["youtube"] and "clé API refusée" in rows["youtube"]
    assert "incomplète" in rows["steam"] and "échéance atteinte" in rows["steam"]
    assert "pas relevée" in rows["igdb"]  # une source jamais relevée garde sa ligne, dite telle quelle


# --- hauteur : jamais plus de 12 jaquettes sans « voir plus » ---------------------------------------------------

@NODE
def test_gamesHtml_renders_at_most_12_covers_before_voir_plus(tmp_path):
    recent = [_entry(n, 0 if n < 3 else -n) for n in range(1, 21)]
    upcoming = [_entry(100 + n, 1 + n % 14) for n in range(40)]
    data = _data(recent, upcoming)
    html, full = _eval(tmp_path, data, "gamesHtml(D.day.releases, D.day.date, 14, false)",
                       "gamesHtml(D.day.releases, D.day.date, 14, true)")
    band = _between(html, 'class="recent"', 'class="cal-frise"')
    frise = _between(html, 'class="cal-frise"', 'class="mlist"')
    mlist = _between(html, 'class="mlist"')
    assert _covers(band) + _covers(frise) <= 12
    assert _covers(band) + _covers(mlist) <= 12  # le téléphone voit l'agenda à la place de la frise
    assert re.search(r'<button[^>]*data-veille-more="releases"[^>]*>\s*Voir plus', html)
    # « Voir plus » rend tout, et « Voir moins » revient
    assert _covers(_between(full, 'class="recent"', 'class="cal-frise"')) == 20
    assert "Voir moins" in full and "Voir plus" not in full


@NODE
def test_gamesHtml_shows_no_voir_plus_when_everything_fits(tmp_path):
    data = _data([_entry(1, -1), _entry(2, 0)], [_entry(3, 2), _entry(4, 3)])
    (html,) = _eval(tmp_path, data, "gamesHtml(D.day.releases, D.day.date, 14, false)")
    assert "data-veille-more" not in html and "Voir plus" not in html


@NODE
def test_a_day_with_more_sorties_than_the_column_holds_announces_the_rest(tmp_path):
    recent = [_entry(n, -n) for n in range(1, 7)]  # 6 récentes, toutes visibles
    upcoming = [_entry(100 + n, 2) for n in range(10)]  # 10 sorties le même jour, 6 places restent
    (html,) = _eval(tmp_path, _data(recent, upcoming), "gamesHtml(D.day.releases, D.day.date, 14, false)")
    frise = _between(html, 'class="cal-frise"', 'class="mlist"')
    assert "+5 autres" in frise
    assert _covers(_between(html, 'class="recent"', 'class="cal-frise"')) + _covers(frise) <= 12


# --- hauteur : « Ce qui monte » ne rend pas des centaines de lignes d'un coup -----------------------------------

def _games(n: int) -> list[dict]:
    return [{"key": f"jeu {i}", "name": f"Jeu {i}", "vod_count": i, "twitch_delta_pct": 100 - i} for i in range(n)]


@NODE
def test_rising_table_shows_the_ten_strongest_rows_then_voir_plus_and_voir_moins(tmp_path):
    data = _data()
    data["day"]["games"] = _games(40)
    short, full = _eval(tmp_path, data, "veilleRising(D)", "(veilleUi.more.rising = true, veilleRising(D))")
    assert len(re.findall(r"<tbody>.*?</tbody>", short, re.S)) == 1
    assert short.count("<tr>") - 1 == 10  # 10 lignes + l'en-tête
    assert short.index("Jeu 0") < short.index("Jeu 9") and "Jeu 10<" not in short  # les plus fortes d'abord
    assert re.search(r'<button[^>]*data-veille-more="rising"[^>]*>\s*Voir plus \(30 jeux\)', short)
    assert full.count("<tr>") - 1 == 40 and "Voir moins" in full


@NODE
def test_rising_table_has_no_voir_plus_when_ten_games_or_fewer(tmp_path):
    data = _data()
    data["day"]["games"] = _games(10)
    (html,) = _eval(tmp_path, data, "veilleRising(D)")
    assert "data-veille-more" not in html
