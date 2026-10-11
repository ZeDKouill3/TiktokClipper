"""Écran Vidéos v3 (TASK-6047856a5558, ADR-a308, SPEC-7715 E2, T2, T9, T10).

Aucun test ne touche le réseau ni un navigateur : `screens/videos.js` est lu tel quel et exécuté sous node
(quand il est là) avec des doublures minimales de la coquille (`api`, `morph`, `confirmDialog`...).
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

VIDEOS_JS = (STATIC / "screens" / "videos.js").read_text(encoding="utf-8")

STEPS = ["download", "transcribe", "audio", "scenes", "action", "moments", "vision", "parts", "captions",
         "reframe", "subtitles", "render", "qa"]

# Doublures de la coquille (ui.js, screens.js, app.js) : seulement ce que videos.js appelle.
STUBS = r"""
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fr = (n, d) => Number(n).toLocaleString('fr-FR', {minimumFractionDigits: d || 0, maximumFractionDigits: d || 0});
const icon = (n, cls) => `<svg class="i ${cls || ''}" data-name="${n}"></svg>`;
const fmtParis = (iso) => 'PARIS<' + iso + '>';
const CLIPPER_TZ = 'Europe/Paris';
const $ = () => null, $$ = () => [];
const document = { addEventListener() {} };
const location = { hash: '' };
const history = { replaceState() {}, pushState() {} };
const STEP_LABELS = { download: 'Téléchargement', transcribe: 'Transcription', audio: 'Audio', scenes: 'Plans',
  action: 'Action', moments: 'Moments', vision: 'Images', parts: 'Découpage', captions: 'Légendes',
  reframe: 'Recadrage', subtitles: 'Sous-titres', render: 'Rendu', qa: 'Contrôle qualité' };
const STATUS_LABELS = { pending: 'en attente', running: 'en cours', done: 'terminée', awaiting_review: 'à valider',
  queued: 'en file', failed: 'échec' };
const store = { videos: null, queue: null, channels: [], dashboard: null };
let currentScreen = 'videos';
const Screens = {};
let painted = null;
const morph = (el, html) => { painted = html; };
const videoThumb = (id) => `<span class="job-thumb" data-thumb="${id}"></span>`;
const emptyState = (ic, title, text, action) => `<div class="empty"><h3>${title}</h3><p>${text}</p>${action || ''}</div>`;
const juryPanelHtml = () => '<div class="jury"></div>';
const jsonBody = (method, body) => ({ method, body: JSON.stringify(body) });
const calls = [], toasts = [], errors = [];
const api = (url, opts) => {
  calls.push({ url, opts });
  if (url.startsWith('/api/videos?')) return Promise.resolve([]);
  return Promise.resolve({});
};
const confirmDialog = async () => true;
const toast = (o) => toasts.push(o);
const toastError = (title, err) => errors.push({ title, message: String(err && err.message || err) });
const loadVideos = async () => {}, loadQueue = async () => {}, updateCounts = () => {}, renderCurrent = () => {};
const wireActions = () => {};
const openAssignChannel = () => {};
const copyText = () => {};
const drawer = { open() {}, close() {}, isOpen: () => false };
const openDialog = () => {};
const statusChip = (s) => `<span class="chip ${s}">${STATUS_LABELS[s] || s}</span>`;
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


def _run(expr: str):
    """Évalue `expr` après le script de l'écran (les doublures d'abord) ; renvoie son JSON."""
    script = STUBS + VIDEOS_JS + f"\nconsole.log(JSON.stringify({expr}));"
    return json.loads(_node(script))


def _steps(done: int, running: str | None = None, failed: str | None = None) -> dict:
    out = {}
    for index, name in enumerate(STEPS):
        status = "done" if index < done else "pending"
        if name == running:
            status = "running"
        if name == failed:
            status = "failed"
        out[name] = {"status": status}
    if running:
        out[running]["progress"] = {"fraction": 0.4, "eta_s": 120}
    return out


def _video(**over) -> dict:
    video = {"video_id": "abcdefghijk", "title": "Titre de la vidéo", "channel": "ma_chaine", "status": "running",
             "current_step": "transcribe", "steps": _steps(1, running="transcribe"), "clips": [],
             "updated_at": "2026-10-11T08:00:00+00:00", "platform_thumbnail": None, "durations": {"download": 30.0},
             "source_url": "https://example.test/watch?v=abcdefghijk"}
    video.update(over)
    return video


# --- Clause 1 : videoRow(v) rend une <tr> avec statut, étape et barre ----------------------------------------------


@NODE
def test_video_row_is_a_table_row_with_status_step_and_progress_bar():
    html = _run(f"videoRow({json.dumps(_video())})")

    assert html.lstrip().startswith("<tr") and html.rstrip().endswith("</tr>")
    assert 'data-video="abcdefghijk"' in html
    assert "Titre de la vidéo" in html and "ma_chaine" in html
    assert 'class="chip accent"' in html and "en cours" in html  # statut
    assert "Transcription" in html and "40 %" in html              # étape courante et sa fraction
    assert 'class="bar"' in html and "--p:0.4" in html             # barre de progression (transform, pas width)


@NODE
def test_video_row_of_a_done_video_counts_steps_and_clips_without_a_bar():
    done = _video(status="done", current_step=None, steps=_steps(13), clips=[{}, {}, {}])
    html = _run(f"videoRow({json.dumps(done)})")

    assert "13/13 étapes" in html and 'class="chip ok"' in html
    assert 'class="bar"' not in html
    assert re.search(r'data-l="Clips"[^>]*>3<', html)


@NODE
def test_video_row_of_a_failed_video_shows_the_reason_in_place_and_escapes_html():
    failed = _video(status="failed", current_step="render", steps=_steps(11, failed="render"),
                    reason="ffmpeg a échoué : <code 1>", title="<b>x</b>")
    html = _run(f"videoRow({json.dumps(failed)})")

    assert "ffmpeg a échoué : &lt;code 1&gt;" in html and "<b>x</b>" not in html
    assert "Rendu" in html


@NODE
def test_video_row_without_a_style_says_so_and_links_to_the_sheet():
    html = _run(f"videoRow({json.dumps(_video(channel=None))})")

    assert "sans style" in html
    assert 'href="#/videos/abcdefghijk"' in html


# --- Clause 2 : journalRow(e) colore le niveau ---------------------------------------------------------------------


@NODE
def test_journal_row_colours_each_level_and_keeps_the_message():
    rows = {lvl: _run(f"journalRow({json.dumps({'at': '2026-10-11T08:00:00+00:00', 'level': lvl, 'step': 'render', 'message': 'm <x>'})})")
            for lvl in ("INFO", "WARNING", "ERROR")}

    assert all(r.lstrip().startswith("<tr") for r in rows.values())
    assert 'class="lvl info"' in rows["INFO"]
    assert 'class="lvl warning"' in rows["WARNING"]
    assert 'class="lvl error"' in rows["ERROR"]
    assert "m &lt;x&gt;" in rows["ERROR"] and "render" in rows["ERROR"]
    assert re.search(r'<td class="mono">10:00:00</td>', rows["INFO"])  # heure de Paris seule, pas de date


# --- Clause 3 : filtres serveur conservés (?channel&status&q) -------------------------------------------------------


@NODE
def test_filters_are_sent_to_the_server_as_channel_status_and_q():
    script = STUBS + VIDEOS_JS + """
(async () => {
  videosSetFilter('channel', 'ma_chaine'); videosSetFilter('status', 'failed'); videosSetFilter('q', 'abc def');
  await videosLoad();
  console.log(JSON.stringify(calls.map((c) => c.url).filter((u) => u.startsWith('/api/videos?'))));
})();"""
    urls = json.loads(_node(script))

    assert urls and urls[-1] == "/api/videos?channel=ma_chaine&status=failed&q=abc+def"


@NODE
def test_no_filter_means_no_query_string_parameter():
    script = STUBS + VIDEOS_JS + """
(async () => { await videosLoad(); console.log(JSON.stringify(calls.map((c) => c.url))); })();"""

    assert json.loads(_node(script))[0] == "/api/videos?"


# --- Clause 4 : rubricLabel(path) ne contient jamais de chemin absolu ----------------------------------------------


@NODE
@pytest.mark.parametrize("path, expected", [
    ("builtin", "Standard"),
    ("builtin:gaming", "Gaming"),
    ("builtin:gaming-action", "Gaming action"),
    ("C:\\Users\\ma_chaine\\clipper\\rubrics\\ma_grille.md", "Fichier personnalisé (ma_grille.md)"),
    ("/home/compte_alpha/rubrics/ma_grille.md", "Fichier personnalisé (ma_grille.md)"),
    ("rubrics/ma_grille.md", "Fichier personnalisé (ma_grille.md)"),
])
def test_rubric_label_names_the_rubric_and_never_leaks_a_path(path, expected):
    label = _run(f"rubricLabel({json.dumps(path)})")

    assert label == expected
    assert "\\" not in label and "/" not in label and "C:" not in label


# --- Clause 5 : le stepper rend 13 button.step ---------------------------------------------------------------------


@NODE
def test_stepper_renders_one_button_per_pipeline_step_in_order():
    html = _run(f"stepperHtml({json.dumps(_video())}, 'transcribe')")

    buttons = re.findall(r'<button[^>]*class="step[ "][^>]*>', html)
    assert len(buttons) == 13
    assert re.findall(r'data-step="(\w+)"', html) == STEPS
    pressed = [b for b in re.findall(r"<button[^>]*>", html) if 'aria-pressed="true"' in b]
    assert len(pressed) == 1 and 'data-step="transcribe"' in pressed[0]
    assert 'class="step done"' in html and 'class="step running' in html and 'class="step pending"' in html


# --- Clause 6 : plus de data-add-form (ajout par le dialogue global) -----------------------------------------------


def test_videos_screen_has_no_add_form_and_opens_the_global_add_dialog():
    assert "data-add-form" not in VIDEOS_JS and "vadd" not in VIDEOS_JS
    assert "data-add" in VIDEOS_JS and "Ajouter une vidéo" in VIDEOS_JS  # app.js branche [data-add] sur openAddVideo


# --- Fiche et liste rendues par l'écran : tout par morph, jamais innerHTML -----------------------------------------


@NODE
def test_list_render_paints_a_table_with_filters_count_and_one_row_per_video():
    script = STUBS + VIDEOS_JS + f"""
(async () => {{
  store.videos = {json.dumps([_video()])};
  videosState.list = {json.dumps([_video(), _video(video_id='bbbbbbbbbbb', status='done', steps=_steps(13), current_step=None)])};
  videosState.disk = {{ workspace_bytes: 2048, output_bytes: 1024 }};
  Screens.videos.render({{ closest: () => null }}, store);
  console.log(JSON.stringify(painted));
}})();"""
    html = json.loads(_node(script))

    assert html.count("<tr") >= 3  # en-tête + 2 lignes
    assert 'type="search"' in html and "data-screen-search" in html  # « / » met le focus dessus (palette, T7)
    assert 'role="radiogroup"' in html and "data-status" in html
    assert "2 vidéos" in html
    assert "data-add-form" not in html


@NODE
def test_detail_render_has_stepper_clips_link_journal_table_and_a_rubric_without_path():
    video = _video(status="done", current_step=None, steps=_steps(13), clips=[{}, {}],
                   rubric="C:\\Users\\ma_chaine\\rubrics\\ma_grille.md", durations={n: 10.0 for n in STEPS})
    script = STUBS + VIDEOS_JS + f"""
(async () => {{
  location.hash = '#/videos/abcdefghijk';
  videosState.detailId = 'abcdefghijk'; videosState.detail = {json.dumps(video)};
  videosState.events = [{{ at: '2026-10-11T08:00:00+00:00', level: 'warning', step: 'qa', message: 'avertissement' }}];
  Screens.videos.render({{ closest: () => null }}, store);
  console.log(JSON.stringify(painted));
}})();"""
    html = json.loads(_node(script))

    assert len(re.findall(r'<button[^>]*class="step[ "]', html)) == 13
    assert 'class="lvl warning"' in html and "<table" in html  # journal en table
    assert "Fichier personnalisé (ma_grille.md)" in html
    assert "C:\\" not in html and "Users" not in html
    assert 'href="#/clips/abcdefghijk"' in html
    assert "data-add-form" not in html


# --- Contrats gardés de l'ancien écran (équivalents nommés) --------------------------------------------------------


def test_videos_screen_keeps_the_server_order_and_never_sorts_client_side():
    assert ".sort(" not in VIDEOS_JS


def test_videos_screen_is_painted_by_morph_only():
    assert "innerHTML" not in re.sub(r"/\*.*?\*/", "", VIDEOS_JS, flags=re.S)


@NODE
def test_empty_list_offers_the_add_button_that_opens_the_global_dialog():
    script = STUBS + VIDEOS_JS + """
(() => {
  store.videos = []; videosState.list = [];
  Screens.videos.render({ closest: () => null }, store);
  console.log(JSON.stringify(painted));
})();"""
    html = json.loads(_node(script))

    assert "data-add>" in html and "Ajouter une vidéo" in html and "data-add-form" not in html
