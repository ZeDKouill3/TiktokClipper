"""Écran Revue v3 (TASK-f4b50350d8d1, ADR-a308, SPEC-7715 E3).

Aucun test ne touche le réseau ni un navigateur : `screens/review.js` est lu tel quel et exécuté sous node
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

REVIEW_JS = (STATIC / "screens" / "review.js").read_text(encoding="utf-8")

STUBS = r"""
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fr = (n, d) => Number(n).toLocaleString('fr-FR', {minimumFractionDigits: d || 0, maximumFractionDigits: d || 0});
const icon = (n, cls) => `<svg class="i ${cls || ''}" data-name="${n}"></svg>`;
let played = 0;
const node = () => ({ addEventListener() {}, dataset: {}, paused: true, currentTime: 0, readyState: 0,
  play() { played += 1; return Promise.resolve(); }, pause() {} });
const $ = () => node(), $$ = () => [];
const handlers = {};
const document = { addEventListener(name, fn) { handlers[name] = fn; }, querySelector: () => null };
const location = { hash: '' };
const STATUS_LABELS = { pending: 'en attente', running: 'en cours', awaiting_review: 'à valider', done: 'terminée' };
const statusChip = (s) => `<span class="chip ${s}">${STATUS_LABELS[s] || s}</span>`;
const store = { videos: [] };
let currentScreen = 'review';
const Screens = {};
let painted = null;
const morph = (el, html) => { painted = html; };
const emptyState = (ic, title, text, action) => `<div class="empty"><h3>${title}</h3><p>${text}</p>${action || ''}</div>`;
const juryConfidenceHtml = (c) => `<b class="num">${c} %</b>`;
const jsonBody = (method, body) => ({ method, body: JSON.stringify(body) });
const calls = [], toasts = [];
const api = (url, opts) => {
  if (url.endsWith('/moments')) return Promise.resolve(rv.moments);   // rechargement après une décision
  calls.push({ url, body: opts && opts.body });
  return Promise.resolve({});
};
const confirmDialog = async () => true;
const toast = (o) => toasts.push(o);
const toastError = () => {};
const reloadVideo = async () => {};
let renders = 0;
const renderCurrent = () => { renders += 1; };
"""

MOMENTS = [
    {"id": 0, "start": 10.0, "end": 30.0, "source_duration": 600.0, "score": 82, "hook_text": "Accroche <b>0</b>",
     "justification": "Montée en tension", "transcript": "Texte du moment", "confidence": 80, "judge_confidences": {},
     "preview_url": "/media/source/v1", "parts_total": 1, "decision": None},
    {"id": 1, "start": 100.0, "end": 130.0, "source_duration": 600.0, "score": 71, "hook_text": "Accroche 1",
     "justification": "Chute", "transcript": "", "transcript_error": "transcript : fichier absent",
     "confidence": None, "judge_confidences": {}, "preview_url": "/media/source/v1", "parts_total": 2, "decision": None},
]


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


def _run(body: str):
    """Exécute `body` (async) après le script de l'écran et ses doublures ; renvoie le JSON qu'il imprime."""
    setup = (f"rv.videoId = 'v1'; rv.moments = {json.dumps(MOMENTS)}; rv.cur = 0; rvResetDraft(); rv.body = {{}};\n"
             "store.videos = [{video_id: 'v1', status: 'awaiting_review', awaiting: [0, 1]}];\n")
    script = (STUBS + REVIEW_JS + "\n(async () => {\n" + setup + body + "\n})();")
    return json.loads(_node(script))


# --- rendu par morph : plus aucun rvPaint* ni innerHTML à la main ----------------------------------------------------

def test_review_screen_paints_by_morph_and_no_longer_by_hand():
    assert not re.search(r"rvPaint", REVIEW_JS)
    assert not re.search(r"\.innerHTML\s*=", REVIEW_JS)
    assert not re.search(r"\.(textContent|outerHTML)\s*=", REVIEW_JS)
    assert "morph(body," in REVIEW_JS


def test_review_block_left_style_css_and_lives_in_v3_css():
    assert "/* >>> review */" not in (STATIC / "style.css").read_text(encoding="utf-8")
    css = (STATIC / "v3.css").read_text(encoding="utf-8")
    for selector in (".v3 .rv-layout", ".v3 .rv-item", ".v3 .sel-range", ".v3 .decide", ".v3 .tl-zoom"):
        assert selector in css, selector
    assert len(css.encode("utf-8")) <= 124_162  # budget de v3.css


# --- état vide : cite le mode review et liste les vidéos candidates --------------------------------------------------

@NODE
def test_empty_state_cites_review_mode_and_lists_the_candidate_videos():
    videos = [
        {"video_id": "run00000001", "title": "Une VOD en cours", "status": "running"},
        {"video_id": "que00000002", "title": "Une VOD en file", "status": "queued"},
        {"video_id": "don00000003", "title": "Une VOD finie", "status": "done"},
    ]
    html = _run(f"console.log(JSON.stringify(rvEmptyView({json.dumps(videos)})));")
    assert "review" in html and "auto" in html          # le mode review est cité (et ce que fait auto)
    assert "Aucun moment à valider" in html
    assert "run00000001" in html and "que00000002" in html and "Une VOD en cours" in html
    assert 'href="#/videos/run00000001"' in html
    assert "don00000003" not in html                    # une vidéo terminée n'est pas candidate


@NODE
def test_empty_state_without_candidate_says_so():
    html = _run("console.log(JSON.stringify(rvEmptyView([])));")
    assert "review" in html and "Aucune vidéo en cours de traitement" in html and "<table" not in html


@NODE
def test_screen_renders_the_empty_state_when_no_video_awaits_a_decision():
    out = _run("""
      store.videos = [{video_id: 'abc', title: 'En cours', status: 'running'}];
      rv.videoId = null; rv.moments = null;
      Screens.review.render({ closest: () => null }, store);
      console.log(JSON.stringify(painted));""")
    assert "Aucun moment à valider" in out and "abc" in out


# --- « lancer le rendu » désactivé avec la raison affichée -----------------------------------------------------------

@NODE
def test_render_button_is_disabled_with_the_reason_while_moments_await_a_decision():
    html = _run("console.log(JSON.stringify(rvToolbar([{video_id: 'v1'}])));")
    button = re.search(r'<button[^>]*id="rv-render"[^>]*>', html).group(0)
    assert " disabled" in button and 'aria-describedby="rv-reason"' in button
    assert 'id="rv-reason"' in html
    assert "2 moments sans décision (#0, #1)" in html


@NODE
def test_render_button_is_enabled_and_shows_no_reason_once_everything_is_decided():
    html = _run("""
      store.videos = [{video_id: 'v1', status: 'awaiting_review', awaiting: []}];
      console.log(JSON.stringify(rvToolbar([{video_id: 'v1'}])));""")
    button = re.search(r'<button[^>]*id="rv-render"[^>]*>', html).group(0)
    assert " disabled" not in button
    assert "rv-reason" not in html


@NODE
def test_render_is_refused_with_a_toast_when_moments_still_await():
    out = _run("""
      await rvRender();
      console.log(JSON.stringify({ calls, toast: toasts[0].title }));""")
    assert out["calls"] == [] and out["toast"] == "Rendu impossible"


# --- le lecteur survit : le temps de lecture ne change que le texte et la tête de lecture ----------------------------

@NODE
def test_playback_time_changes_the_clock_and_playhead_but_keeps_the_same_player_markup():
    out = _run("""
      const a = rvView([{video_id: 'v1'}]);
      rv.t = 20;
      const b = rvView([{video_id: 'v1'}]);
      console.log(JSON.stringify({ a, b }));""")
    player = re.compile(r'<video id="rv-media"[^>]*></video>')
    assert player.search(out["a"]).group(0) == player.search(out["b"]).group(0)
    assert out["a"] != out["b"]
    assert 'src="/media/source/v1"' in out["a"]


@NODE
def test_view_exposes_bounds_handles_jury_justification_and_escapes_the_hook():
    html = _run("console.log(JSON.stringify(rvView([{video_id: 'v1'}])));")
    assert 'data-handle="start"' in html and 'data-handle="end"' in html
    assert 'data-field="start"' in html and 'data-field="end"' in html and 'value="10"' in html
    assert "Montée en tension" in html and "Texte du moment" in html
    assert "Accroche &lt;b&gt;0&lt;/b&gt;" in html and "<b>0</b>" not in html
    assert '<button type="button" class="btn" data-variant="outline" id="rv-adjust" disabled' in html  # bornes d'origine


@NODE
def test_missing_transcript_and_duration_are_shown_not_replaced():
    html = _run("rv.cur = 1; rvResetDraft(); console.log(JSON.stringify(rvView([{video_id: 'v1'}])));")
    assert "transcript : fichier absent" in html
    out = _run("""
      rv.moments[0].source_duration = null; rv.moments[0].source_duration_error = 'durée : source introuvable';
      rvResetDraft();
      console.log(JSON.stringify(rvView([{video_id: 'v1'}])));""")
    assert "durée : source introuvable" in out


# --- raccourcis A R J K espace inchangés -----------------------------------------------------------------------------

def _key(key: str) -> str:
    return f"""
      {{
        const ev = {{ key: {json.dumps(key)}, target: {{ tagName: 'BODY' }}, preventDefault() {{}} }};
        handlers.keydown(ev);
        await new Promise((r) => setTimeout(r, 0));
      }}
    """


@NODE
def test_key_a_accepts_the_current_moment_and_key_r_rejects_it():
    out = _run(_key("a") + "console.log(JSON.stringify(calls));")
    assert len(out) == 1 and out[0]["url"] == "/api/videos/v1/moments/0/decide"
    assert json.loads(out[0]["body"]) == {"decision": "accepted"}
    out = _run(_key("R") + "console.log(JSON.stringify(calls));")
    assert json.loads(out[0]["body"]) == {"decision": "rejected"}


@NODE
def test_keys_j_and_k_walk_the_moments_and_wrap_around():
    out = _run(_key("j") + "const a = rv.cur;" + _key("j") + "const b = rv.cur;" + _key("k") + "console.log(JSON.stringify([a, b, rv.cur]));")
    assert out == [1, 0, 1]


@NODE
def test_space_toggles_playback_and_every_handled_key_is_prevented_but_typing_is_left_alone():
    out = _run("""
      const seen = [];
      for (const key of ['a', 'r', 'j', 'k', ' ']) {
        const ev = { key, target: { tagName: 'BODY' }, preventDefault() { seen.push(key); } };
        handlers.keydown(ev);
      }
      const typing = { key: 'a', target: { tagName: 'INPUT' }, preventDefault() { seen.push('typing'); } };
      handlers.keydown(typing);
      const other = { key: 'x', target: { tagName: 'BODY' }, preventDefault() { seen.push('x'); } };
      handlers.keydown(other);
      console.log(JSON.stringify({ seen, played, decisions: calls.length }));""")
    assert out["seen"] == ["a", "r", "j", "k", " "]   # rien pour une saisie ni pour une autre touche
    assert out["played"] == 1                         # espace lance la lecture
    assert out["decisions"] == 1                      # 'a' envoie sa décision, 'r' attend qu'elle finisse (busy)
