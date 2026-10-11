"""Journal v3 (TASK-0c4a2f5767f4, ADR-a308, SPEC-7715 E11).

Aucun test ne touche le réseau ni un navigateur : `screens/journal.js` est lu tel quel et exécuté sous node
(quand il est là) avec des doublures minimales de la coquille (`api`, `morph`, `emptyState`...).
Fixtures neutres : chaîne `ma_chaine`, lignes de journal fictives.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "clipper" / "web" / "static"
NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")

JOURNAL_JS = (STATIC / "screens" / "journal.js").read_text(encoding="utf-8")
V3_CSS = (STATIC / "v3.css").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "style.css").read_text(encoding="utf-8")

STUBS = r"""
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = (n, cls) => `<svg class="i ${cls || ''}" data-name="${n}"></svg>`;
const emptyState = (ic, title, text) => `<div class="empty"><h3>${title}</h3><p>${text}</p></div>`;
const Screens = {};
let currentScreen = 'journal';
const renderCurrent = () => {};
let responses = {};
const api = (url) => {
  const key = Object.keys(responses).find((k) => url.startsWith(k));
  if (!key) return Promise.reject(new Error('HTTP 404 pour ' + url));
  return Promise.resolve(responses[key]);
};
const morph = (el, html) => { el.painted = html; };
const body = { set innerHTML(v) { throw new Error('innerHTML à la main : le rendu passe par morph()'); } };
"""

LINES = {
    "available": True,
    "path": "logs/journal-2026-10-11.log",
    "lines": [
        {"timestamp": "2026-10-11 10:00:00", "process": "run[1]", "level": "INFO", "logger": "clipper.web.app",
         "message": "GET /api/videos 200"},
        {"timestamp": "2026-10-11 10:01:00", "process": "run[2]", "level": "ERROR", "logger": "clipper.render",
         "message": "Rendu de ma_chaine_clip_01 interrompu : une très longue raison qui doit pouvoir passer à la ligne"},
        {"timestamp": "2026-10-11 10:02:00", "process": "run[2]", "level": "WARNING", "logger": "clipper.llm",
         "message": "Réponse partielle du juge compte_alpha"},
    ],
}


def _node(script: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.js"
        path.write_text(script, encoding="utf-8")
        done = subprocess.run(["node", path], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    return done.stdout.strip().splitlines()[-1]


def _run(expr: str, setup: str = "") -> object:
    """Évalue `expr` (async accepté) après journal.js ; renvoie son JSON."""
    script = (STUBS + JOURNAL_JS + "\n(async () => {\n" + setup +
              f"\nconsole.log(JSON.stringify(await ({expr})));\n}})().catch((e) => {{ console.error(e); process.exit(1); }});")
    return json.loads(_node(script))


def _with_data(data: dict) -> str:
    return f"jrnUi.data = {json.dumps(data)};"


# --- rendu : table v3, message replié, niveau coloré ----------------------------------------------------------

@NODE
def test_rows_are_a_table_whose_message_cell_is_wrap_and_whose_levels_are_coloured():
    html = _run("jrnHtml()", _with_data(LINES))
    assert '<table class="table journal">' in html
    assert html.count('<td class="wrap"') == 3
    assert '<span class="lvl error">ERROR</span>' in html
    assert '<span class="lvl warning">WARNING</span>' in html
    assert '<span class="lvl">INFO</span>' in html


@NODE
def test_the_most_recent_line_comes_first():
    html = _run("jrnHtml()", _with_data(LINES))
    assert html.index("Rendu de ma_chaine_clip_01") < html.index("GET /api/videos 200")


@NODE
def test_toolbar_has_a_text_filter_and_a_level_filter_that_reach_the_query():
    html = _run("jrnHtml()", _with_data(LINES))
    assert 'id="jrn-q"' in html and 'id="jrn-level"' in html
    query = _run("jrnQuery()", "jrnUi.q = 'ma_chaine'; jrnUi.level = 'ERROR';")
    assert query == "lines=300&q=ma_chaine&level=ERROR"


@NODE
def test_empty_filter_says_no_line_matches_instead_of_an_empty_table():
    html = _run("jrnHtml()", _with_data({"available": True, "path": "p", "lines": []}))
    assert "Aucune ligne ne correspond au filtre." in html and "<table" not in html


@NODE
def test_screen_paints_its_body_through_morph_inside_a_v3_root():
    painted = _run("(() => { const el = {}; Screens.journal.render(el); return el.painted; })()",
                   _with_data(LINES))
    assert painted.startswith('<div class="v3 journal">') and '<table class="table journal">' in painted


def test_journal_js_has_no_manual_inner_html_rendering():
    assert not re.search(r"\binnerHTML\s*=", JOURNAL_JS)
    assert "morph(" in JOURNAL_JS


# --- CSS : table fixe, message qui passe à la ligne, aucun nowrap sur le message -----------------------------

def test_journal_table_is_fixed_layout_and_its_message_cell_wraps():
    assert re.search(r"\.v3\.journal \.table\s*\{[^}]*table-layout:\s*fixed", V3_CSS)
    assert re.search(r"\.v3 \.table td\.wrap\s*\{[^}]*white-space:\s*normal", V3_CSS)


def test_no_journal_rule_forces_nowrap_on_the_message_cell():
    for rule in re.findall(r"([^{}]*\.journal[^{}]*)\{([^}]*)\}", V3_CSS):
        selector, body_css = rule
        if "nowrap" in body_css:
            assert "wrap" not in selector and "message" not in selector, selector


def test_journal_block_left_style_css_and_is_delimited_in_v3_css():
    assert "/* >>> journal */" not in STYLE_CSS and "#screen-journal" not in STYLE_CSS
    assert "/* >>> journal v3 */" in V3_CSS and "/* <<< journal v3 */" in V3_CSS
