"""Statistiques v3 (TASK-cda961181d07, ADR-a308, SPEC-7715 E7).

Aucun test ne touche le réseau ni un navigateur : `screens/stats.js` est lu tel quel et exécuté sous node
(quand il est là) avec des doublures minimales de la coquille (`api`, `morph`, `confirmDialog`...).
Fixtures neutres : compte `compte_alpha`, vidéos `ma_chaine_*`.
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

STATS_JS = (STATIC / "screens" / "stats.js").read_text(encoding="utf-8")
V3_CSS = (STATIC / "console.css").read_text(encoding="utf-8")

STUBS = r"""
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fr = (n, d) => Number(n).toLocaleString('fr-FR', {minimumFractionDigits: d || 0, maximumFractionDigits: d || 0});
const icon = (n, cls) => `<svg class="i ${cls || ''}" data-name="${n}"></svg>`;
const fmtParis = (iso) => 'PARIS<' + iso + '>';
const $ = () => null, $$ = () => [];
const document = { addEventListener() {} };
const window = { addEventListener() {} };
const location = { hash: '#/stats/compte_alpha' };
const STEP_LABELS = { transcribe: 'Transcription', reframe: 'Recadrage', render: 'Rendu' };
const Screens = {};
let currentScreen = 'stats';
let painted = null;
const morph = (el, html) => { painted = html; };
const emptyState = (ic, title, text) => `<div class="empty"><h3>${title}</h3><p>${text}</p></div>`;
const localStorage = { m: {}, getItem(k) { return k in this.m ? this.m[k] : null; }, setItem(k, v) { this.m[k] = String(v); } };
const toasts = [], errors = [];
const toast = (o) => toasts.push(o);
const toastError = (title, err) => errors.push({ title, message: String(err && err.message || err) });
const confirmDialog = async () => true;
const renderCurrent = () => {};
let responses = {};
const api = (url) => {
  const key = Object.keys(responses).find((k) => url.startsWith(k));
  if (!key) return Promise.reject(new Error('HTTP 404 pour ' + url));
  const out = responses[key];
  return out instanceof Error ? Promise.reject(out) : Promise.resolve(out);
};
const body = { set innerHTML(v) { throw new Error('innerHTML à la main : le rendu passe par morph()'); } };

const ACCOUNT = { account: 'compte_alpha', label: 'Compte alpha', ready: true, refreshing: false, fetched_at: '2026-10-10T08:00:00+00:00', snapshots: 3, full_pages: 8, known_posts: 2 };
const TILE_NULL = { value: null, change_pct: null, history_change_pct: null };
const TILES = { views: { value: 1234, change_pct: 12.5, history_change_pct: null }, profile_views: TILE_NULL, likes: TILE_NULL, comments: TILE_NULL, shares: TILE_NULL };
const series = (n) => ({ labels: ['2026-10-01', '2026-10-02'], values: [n, null], previous: [null, null] });
const OVERVIEW = { account: 'compte_alpha', period: 28, tiles: TILES, series: { views: series(1), profile_views: series(null), likes: series(null), comments: series(null), shares: series(null) } };
const MEASURES = {
  llm_cost: { total: 1.5, unreported_calls: 1, by_usage: { moments: 1.25, captions: 0.25 },
    by_video: { ma_chaine_01: { cost: 1.25, calls: 4, unreported_calls: 0 }, ma_chaine_02: { cost: 0.25, calls: 2, unreported_calls: 1 } } },
  steps: { transcribe: { mean_s: 125, last_s: 90, count: 2 }, render: { mean_s: 45, last_s: 50, count: 2 } },
  counts: { done: 2 },
};
const LEARNING = { enabled: true, sync: { last_sync: null, excluded: [] }, links: { counts: {}, unlinked: [] },
  weights: { judges: {} }, retention: { n: 0, rows: [], message: null }, breakdown: { n: 0, groups: {}, skipped: 0 },
  coach: [{ judge: 'rigueur', version: 2, status: 'proposed', metric: null, perspective_current: 'a', perspective_proposed: 'b' }] };
"""


def _node(script: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        path = handle.name
    try:
        done = subprocess.run(["node", path], capture_output=True, text=True, encoding="utf-8")
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _run(expr: str, setup: str = ""):
    """Évalue `expr` (async accepté) après stats.js ; renvoie son JSON."""
    script = (STUBS + STATS_JS + "\n(async () => {\n" + setup +
              f"\nconsole.log(JSON.stringify(await ({expr})));\n}})().catch((e) => {{ console.error(e); process.exit(1); }});")
    return json.loads(_node(script))


FRESH = "statsUi.accounts = [ACCOUNT]; statsUi.overview = OVERVIEW; statsUi.key = statsKey(); statsUi.at = Date.now();"


# --- critère : aucune tuile ne rend « — » ---------------------------------------------------------------------

@NODE
def test_tile_value_null_says_not_given_by_tiktok_studio_instead_of_a_dash():
    assert _run("[tileValue(null), tileValue(undefined)]") == ["non fourni par TikTok Studio"] * 2


@NODE
def test_tile_value_formats_a_given_number_and_keeps_a_real_zero():
    assert _run("[tileValue(1234), tileValue(0)]") == ["1 234", "0"]


@NODE
def test_overview_tiles_never_render_a_dash_when_tiktok_gives_nothing():
    html = _run("statsOverview(ACCOUNT)", FRESH)
    tiles = html[html.index("data-stats-tiles"):html.index("data-stats-curve")]
    assert tiles.count("non fourni par TikTok Studio") == 4  # 4 tuiles sur 5 sans valeur
    assert "—" not in tiles
    assert "1 234" in tiles


# --- critère : sections apprentissage en .card ------------------------------------------------------------------

@NODE
def test_learning_sections_are_cards_not_legacy_panels():
    html = _run("statsLearningSection()", "statsUi.learning = LEARNING;")
    for marker in ("data-learning-state", "data-learning-weights", "data-learning-retention", "data-learning-breakdown",
                   'data-learning-proposal="rigueur/2"'):
        block = re.search(rf'<div class="([^"]*)"[^>]*{re.escape(marker)}', html)
        assert block and "card" in block.group(1).split(), marker
    assert 'class="panel' not in html


# --- critère : coût par vidéo et durée par étape présents ici -----------------------------------------------------

@NODE
def test_cost_per_video_is_listed_on_the_stats_screen():
    html = _run("statsMeasuresSection()", "statsUi.measures = MEASURES;")
    assert "data-measures-cost" in html
    row1 = re.search(r"<tr[^>]*>(?:(?!</tr>).)*ma_chaine_01(?:(?!</tr>).)*</tr>", html).group(0)
    assert "1,25 $" in row1 and ">4<" in row1
    row2 = re.search(r"<tr[^>]*>(?:(?!</tr>).)*ma_chaine_02(?:(?!</tr>).)*</tr>", html).group(0)
    assert "0,25 $" in row2 and "1 appel sans coût rapporté" in row2


@NODE
def test_step_durations_are_listed_on_the_stats_screen_with_their_french_label():
    html = _run("statsMeasuresSection()", "statsUi.measures = MEASURES;")
    assert "data-measures-steps" in html
    row = re.search(r"<tr[^>]*>(?:(?!</tr>).)*Transcription(?:(?!</tr>).)*</tr>", html).group(0)
    assert "2:05" in row and "1:30" in row and ">2<" in row  # moyenne, dernière, nombre de vidéos


@NODE
def test_measures_read_failure_is_shown_never_hidden():
    html = _run("statsMeasuresSection()", "statsUi.measuresError = new Error('HTTP 500 pour /api/measures');")
    assert 'role="alert"' in html and "HTTP 500 pour /api/measures" in html


@NODE
def test_measures_without_any_call_say_so_instead_of_an_empty_table():
    html = _run("statsMeasuresSection()", "statsUi.measures = { llm_cost: { total: 0, unreported_calls: 0, by_usage: {}, by_video: {} }, steps: {}, counts: {} };")
    assert "Aucun appel au modèle de langage" in html and "Aucune étape terminée" in html


@NODE
def test_load_fetches_measures_and_the_screen_shows_them():
    out = _run("[statsUi.measures.llm_cost.total, statsUi.measuresError]",
               "responses = { '/api/stats/tiktok/compte_alpha': OVERVIEW, '/api/stats/tiktok': { accounts: [ACCOUNT] }, '/api/learning': LEARNING, '/api/measures': MEASURES };"
               "await statsLoad();")
    assert out == [1.5, None]


# --- rendu par morph, racine v3 -----------------------------------------------------------------------------

@NODE
def test_screen_paints_by_morph_inside_a_v3_root_and_never_by_innerhtml():
    html = _run("(Screens.stats.render(body), painted)", FRESH + "statsUi.measures = MEASURES; statsUi.learning = LEARNING;")
    assert html.startswith('<div class="s3">')
    assert "data-measures-cost" in html and "data-learning-state" in html and "data-stats-tiles" in html
    assert 'class="panel' not in html


@NODE
def test_screen_without_account_is_painted_by_morph_too():
    html = _run("(Screens.stats.render(body), painted)", "statsUi.accounts = []; statsUi.key = statsKey(); statsUi.at = Date.now();")
    assert html.startswith('<div class="s3">') and "Aucun compte TikTok" in html


def test_stats_js_has_no_manual_inner_html_rendering():
    assert not re.search(r"body\.innerHTML\s*=", STATS_JS)


# --- compte affiché par défaut (revue 11/10) ----------------------------------------------------------------------

DEFAULT_SETUP = (
    "location.hash = '#/stats';"
    "const alpha = { ...ACCOUNT, account: 'compte_alpha' }, beta = { ...ACCOUNT, account: 'compte_beta' }, gamma = { ...ACCOUNT, account: 'compte_gamma' };"
    "const withViews = (account, n) => ({ ...OVERVIEW, account, tiles: { ...TILES, views: { value: n, change_pct: null, history_change_pct: null } } });"
    "responses = { '/api/stats/tiktok/compte_alpha': withViews('compte_alpha', 0), '/api/stats/tiktok/compte_beta': withViews('compte_beta', 40),"
    " '/api/stats/tiktok/compte_gamma': withViews('compte_gamma', 7), '/api/stats/tiktok': { accounts: [alpha, beta, gamma] }, '/api/learning': LEARNING, '/api/measures': MEASURES };"
)


@NODE
def test_default_account_is_the_first_one_with_views_not_the_first_alphabetically():
    out = _run("statsAccount().account", DEFAULT_SETUP + "await statsLoad();")
    assert out == "compte_beta"


@NODE
def test_default_account_is_the_last_one_consulted_when_it_still_exists():
    out = _run("statsAccount().account", DEFAULT_SETUP + "localStorage.setItem('clipper-stats-account', 'compte_gamma'); await statsLoad();")
    assert out == "compte_gamma"


@NODE
def test_consulting_an_account_by_the_address_remembers_it_for_next_time():
    out = _run("[localStorage.getItem('clipper-stats-account'), (location.hash = '#/stats', statsAccount().account)]",
               DEFAULT_SETUP + "location.hash = '#/stats/compte_gamma'; await statsLoad();")
    assert out == ["compte_gamma", "compte_gamma"]


@NODE
def test_default_account_is_the_first_one_when_none_has_views():
    out = _run("statsAccount().account", DEFAULT_SETUP.replace("withViews('compte_beta', 40)", "withViews('compte_beta', 0)").replace("withViews('compte_gamma', 7)", "withViews('compte_gamma', 0)") + "await statsLoad();")
    assert out == "compte_alpha"


# --- en-tête de page identique sur tous les écrans (revue 11/10) ----------------------------------------------------

def test_every_screen_header_has_the_same_shape_and_a_section_eyebrow():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    sections = re.findall(r'<section class="screen[^"]*" id="screen-(\w+)"[^>]*>(.*?)</section>', page, flags=re.S)
    heads = {name: re.search(r'<div class="eyebrow">([^<]*)</div><h1>([^<]*)</h1><p class="sub">', body) for name, body in sections if 'class="page-head' in body}
    assert len(heads) == 11, sorted(heads)
    for name, match in heads.items():
        assert match, f"{name} : en-tête hors gabarit (sur-titre, titre, sous-titre)"
        assert match.group(1) in ("Production", "Configuration"), (name, match.group(1))
    assert heads["stats"].group(1) == "Configuration"


# --- CSS : bloc retiré de style.css, ajouté en fin de v3.css, budget -------------------------------------------------

def test_stats_css_lives_in_console_css_and_style_css_is_gone():
    assert not (STATIC / "style.css").exists()
    assert "/* >>> stats */" in V3_CSS and "/* <<< stats */" in V3_CSS


def test_v3_css_ends_with_the_stats_block_and_stays_within_budget():
    assert len(V3_CSS.encode("utf-8")) <= 163_840
    start = V3_CSS.index("/* >>> stats */")
    assert V3_CSS.index("/* <<< stats */") > start
    # bloc ferme, pas forcement le dernier : les ecrans migres ensuite ajoutent le leur a la suite
    assert V3_CSS.index("/* >>> stats */") < V3_CSS.index("/* <<< stats */")
    block = V3_CSS[start:]
    for needle in (".s3 ", ".kpi ", ".s3-card", "@media screen and (max-width: 900px)"):
        assert needle in block, needle
    assert "overflow-x" in block  # tableaux défilants : pas de débordement de page à 390 px
