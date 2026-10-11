"""Écran Clips v3 et fiche clip en tiroir (TASK-1679e58d021b, ADR-a308, SPEC-7715 E4 / E12 / T10 / T11).

Aucun test ne touche le réseau ni un navigateur : les fichiers statiques sont lus tels quels et la logique
pure de l'écran tourne sous node (clips.js évalué entier dans un contexte vm avec des bouchons de page).
Fixtures sans nom réel (ma_chaine, compte_alpha).
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


def _static(*parts: str) -> str:
    return (STATIC / Path(*parts)).read_text(encoding="utf-8")


# Bouchons de page : ce que clips.js attend au chargement (document, window, location, Screens) et les
# briques de ui.js qu'il appelle en rendant (esc, fr, icon, fmtParis, juryConfidenceHtml, emptyState).
_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = vm.createContext({
  console, JSON, Date, Number, Math, Array, Object, Set, String, encodeURIComponent, decodeURIComponent, setTimeout,
  location: { hash: "" }, history: { pushState() {}, replaceState() {} },
  document: { addEventListener() {}, querySelector() { return null; } }, window: { addEventListener() {} },
  Screens: {}, currentScreen: "clips",
  esc: (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  fr: (n, d) => Number(n).toLocaleString("fr-FR", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 }),
  icon: (n, cls) => `<svg class="i ${cls || ""}" data-name="${n}"></svg>`,
  CLIPPER_TZ: "Europe/Paris",
  fmtParis: (iso, opts) => new Date(iso).toLocaleString("fr-FR", Object.assign({ timeZone: "Europe/Paris" }, opts)),
  juryConfidenceHtml: (c) => (c == null ? `<span class="muted">non évaluée (pas de jury pour ce moment)</span>` : `<b class="num">${c} %</b>`),
  emptyState: (i, t, x) => `<div class="empty"><h3>${t}</h3><p>${x}</p></div>`,
  addVideoButton: "",
  drawer: { isOpen() { return false; }, open() {}, close() {} },
});
const src = fs.readFileSync(process.argv[2], "utf8").replace(/^const clipsUi = /m, "var clipsUi = ");
vm.runInContext(src, ctx);
const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
ctx.clipsUi.data = data;
const out = vm.runInContext(fs.readFileSync(process.argv[4], "utf8"), ctx);
process.stdout.write(JSON.stringify(out));
"""


def _clips_eval(expr: str, data: list[dict]):
    """Évalue `expr` dans clips.js chargé entier (clipsUi.data = data) ; renvoie le résultat JSON."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "h.js").write_text(_HARNESS, encoding="utf-8")
        (root / "data.json").write_text(json.dumps(data), encoding="utf-8")
        (root / "expr.js").write_text(expr, encoding="utf-8")
        done = subprocess.run(["node", str(root / "h.js"), str(STATIC / "screens" / "clips.js"), str(root / "data.json"),
                               str(root / "expr.js")], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _clip(clip_id="01", **over) -> dict:
    base = {
        "video_id": "v1", "clip_id": clip_id, "part": 1, "parts_total": 1, "publish_status": "à valider",
        "channel": "ma_chaine", "source_title": "Titre de la VOD", "screen_title": f"« Clip {clip_id} »",
        "title": f"Clip {clip_id}", "caption": "Légende", "description": "Légende", "hashtags": ["#a", "#b"],
        "duration": 31.5, "start": 100.0, "end": 131.5, "score": 56.7, "layout": "stream_split",
        "reason": "Une chute claire.", "hook_text": "Sa promesse",
        "scores": {"hook": 5, "standalone": 7, "payoff": 5, "emotion": 6, "value": 3, "trend": 1},
        "qa_status": "passed", "issues": [], "jury_confidence": None, "jury_judge_confidences": None,
        "video_url": "/media/clip/v1/01", "thumbnail_url": "/media/clip/v1/01/thumbnail", "video_deleted": False,
        "slot_at": None, "publish_error": None,
    }
    base.update(over)
    return base


# --- C1 : onglets sans les statuts à zéro, sauf « À valider » ---------------------------------------------------

@NODE
def test_tabs_omit_statuses_at_zero_but_always_keep_a_valider():
    data = [_clip("01"), _clip("02"), _clip("03", publish_status="published")]
    tabs = _clips_eval('clipsTabs(clipsUi.data, { filter: "à valider", channel: "", video: "" })', data)
    keys = [t[0] for t in tabs]
    assert keys == ["à valider", "published", "all"]
    assert dict((t[0], t[2]) for t in tabs) == {"à valider": 2, "published": 1, "all": 3}
    # aucun clip à valider : l'onglet reste (c'est la décision par défaut), les autres statuts à 0 disparaissent
    tabs = _clips_eval('clipsTabs(clipsUi.data, { filter: "à valider", channel: "", video: "" })',
                       [_clip("01", publish_status="rejected")])
    assert [t[0] for t in tabs] == ["à valider", "rejected", "all"]
    assert tabs[0][2] == 0


@NODE
def test_the_current_tab_stays_visible_even_at_zero_so_the_filter_can_be_left():
    tabs = _clips_eval('clipsTabs(clipsUi.data, { filter: "failed", channel: "", video: "" })', [_clip("01")])
    assert [t[0] for t in tabs] == ["à valider", "failed", "all"]


@NODE
def test_tab_counters_follow_the_video_and_style_filters():
    data = [_clip("01"), _clip("02", video_id="v2", publish_status="published", channel="autre")]
    tabs = _clips_eval('clipsTabs(clipsUi.data, { filter: "à valider", channel: "", video: "v2" })', data)
    assert [(t[0], t[2]) for t in tabs] == [("à valider", 0), ("published", 1), ("all", 1)]


# --- C2 : filtre vidéo par titre -------------------------------------------------------------------------------

@NODE
def test_video_filter_shows_the_title_of_each_video_with_its_id():
    data = [_clip("01"), _clip("02"), _clip("03", video_id="v2", source_title=None)]
    options = _clips_eval('clipsVideoOptions(clipsUi.data, { video: "" })', data)
    assert [o["id"] for o in options] == ["v1", "v2"]
    assert options[0]["label"].startswith("Titre de la VOD") and "v1" in options[0]["label"]
    assert options[1]["label"] == "v2"  # pas de titre dans le sidecar : l'id, jamais un titre inventé


@NODE
def test_video_filter_keeps_the_chosen_video_even_when_none_of_its_clips_is_loaded():
    options = _clips_eval('clipsVideoOptions(clipsUi.data, { video: "v9" })', [_clip("01")])
    assert [o["id"] for o in options] == ["v1", "v9"]


def test_video_select_is_filled_by_the_titled_options():
    js = _static("screens", "clips.js")
    assert 'id="clips-video"' in js and "clipsVideoOptions(" in js
    assert 'id="clips-filter"' in js and "clipsTabs(" in js


# --- C3-C6 : le tiroir ------------------------------------------------------------------------------------------

@NODE
def test_drawer_offers_approve_reject_and_rerender_on_a_clip_to_validate():
    html = _clips_eval('clipDrawerHtml(clipsUi.data[0])', [_clip("01")])
    assert "Approuver" in html and "Refuser" in html and "Re-rendre" in html
    assert "data-approve" in html and "data-reject" in html and "data-rerender" in html
    assert 'id="drawer-title"' in html and "data-drawer-close" in html  # tiroir v3 (aria-labelledby, fermeture)
    assert "<video" in html and "controls" in html


@NODE
def test_drawer_lists_the_six_scores_with_their_values():
    html = _clips_eval('clipDrawerHtml(clipsUi.data[0])', [_clip("01")])
    for key, value in (("hook", 5), ("standalone", 7), ("payoff", 5), ("emotion", 6), ("value", 3), ("trend", 1)):
        assert re.search(rf"<b[^>]*>{value}</b>\s*(<span[^>]*>)?{key}", html), key
    assert html.count('class="score"') == 6


@NODE
def test_drawer_scores_absent_from_the_sidecar_say_so_never_zero():
    html = _clips_eval('clipDrawerHtml(clipsUi.data[0])', [_clip("01", scores=None)])
    assert html.count('class="score"') == 6
    assert "<b>0</b>" not in html and "non noté" in html


@NODE
def test_drawer_shows_qa_warnings_with_type_severity_and_detail():
    issues = [{"type": "weak_hook", "detail": "Aucun texte d'accroche sur l'image 1.", "severity": "warning", "source": "llm"},
              {"type": "starts_mid_sentence", "detail": "On arrive au milieu d'une phrase.", "severity": "warning"}]
    html = _clips_eval('clipDrawerHtml(clipsUi.data[0])', [_clip("01", issues=issues)])
    assert "[object Object]" not in html
    assert "weak_hook" in html and "Aucun texte d&#39;accroche sur l&#39;image 1." in html
    assert "starts_mid_sentence" in html and "On arrive au milieu d&#39;une phrase." in html
    assert "2 avertissements" in html
    clean = _clips_eval('clipDrawerHtml(clipsUi.data[0])', [_clip("01")])
    assert "Aucun problème relevé" in clean and "avertissement" not in clean


@NODE
def test_drawer_says_non_evaluee_when_the_jury_confidence_is_null_and_shows_it_otherwise():
    none = _clips_eval('clipDrawerHtml(clipsUi.data[0])', [_clip("01", jury_confidence=None)])
    assert "non évaluée" in none
    rated = _clips_eval('clipDrawerHtml(clipsUi.data[0])', [_clip("01", jury_confidence=72, jury_judge_confidences={"retention": 90})])
    assert "72 %" in rated and "non évaluée" not in rated


@NODE
def test_drawer_shows_the_position_in_the_list_and_the_series_parts():
    data = [_clip("01-p1", part=1, parts_total=2), _clip("01-p2", part=2, parts_total=2), _clip("02")]
    html = _clips_eval('clipDrawerHtml(clipsUi.data[1])', data)
    assert "2/3" in html                                  # position dans la liste filtrée (j / k)
    assert 'data-part="v1/01-p1"' in html and "partie 2/2" in html
    assert "Fiche complète" in html and "#/clip/v1/01-p2" in html


@NODE
def test_drawer_hides_approve_for_a_published_clip_and_rerender_for_a_deleted_video():
    published = _clips_eval('clipDrawerHtml(clipsUi.data[0])', [_clip("01", publish_status="published")])
    assert "data-approve" not in published
    gone = _clips_eval('clipDrawerHtml(clipsUi.data[0])', [_clip("01", publish_status="published", video_deleted=True)])
    assert "data-rerender" not in gone and "<video" not in gone and "Vidéo supprimée" in gone


# --- C7 : refus d'une partie = série entière, confirmée ---------------------------------------------------------

@NODE
def test_rejecting_a_part_asks_to_confirm_the_whole_series():
    ask = _clips_eval('clipRejectConfirm(clipsUi.data[0])', [_clip("01-p2", part=2, parts_total=3)])
    assert "série entière" in ask["title"] + ask["body"]
    assert "2/3" in ask["body"] and "3 parties" in ask["body"]
    assert "série" in ask["confirmLabel"]
    single = _clips_eval('clipRejectConfirm(clipsUi.data[0])', [_clip("01")])
    assert "série" not in single["title"] + single["body"]


def test_reject_handler_confirms_with_clip_reject_confirm_before_posting():
    js = _static("screens", "clips.js")
    handler = js[js.index('$("[data-reject]", d).onclick'):]
    handler = handler[:handler.index("};\n")]
    assert "confirmDialog(clipRejectConfirm(c))" in handler
    assert handler.index("confirmDialog(") < handler.index('clipUrl(c, "/reject")')


# --- C8 : raccourcis j k a r, écran Clips et tiroir ouvert seulement ----------------------------------------------

@NODE
def test_j_k_a_r_act_only_on_the_clips_screen_with_the_drawer_open():
    out = _clips_eval("""(() => {
      const on = { screen: "clips", drawerOpen: true, typing: false, dialogOpen: false, modifier: false };
      return {
        j: clipsShortcut("j", on), k: clipsShortcut("k", on), a: clipsShortcut("a", on), r: clipsShortcut("r", on),
        other: clipsShortcut("x", on), upper: clipsShortcut("J", on),
        closed: clipsShortcut("j", Object.assign({}, on, { drawerOpen: false })),
        elsewhere: clipsShortcut("a", Object.assign({}, on, { screen: "videos" })),
        typing: clipsShortcut("r", Object.assign({}, on, { typing: true })),
        dialog: clipsShortcut("a", Object.assign({}, on, { dialogOpen: true })),
        ctrl: clipsShortcut("k", Object.assign({}, on, { modifier: true })),
      };
    })()""", [_clip("01")])
    assert (out["j"], out["k"], out["a"], out["r"]) == ("next", "prev", "approve", "reject")
    assert out["other"] is None and out["upper"] is None
    for key in ("closed", "elsewhere", "typing", "dialog", "ctrl"):
        assert out[key] is None, key


def test_the_keydown_listener_is_wired_on_current_screen_clips_and_the_open_drawer():
    js = _static("screens", "clips.js")
    listener = js[js.index("function clipsOnKey"):]
    listener = listener[:listener.index("\n}\n")]
    assert 'currentScreen === "clips"' in listener and "drawer.isOpen()" in listener
    assert 'document.addEventListener("keydown", clipsOnKey)' in js
    for action in ("[data-approve]", "[data-reject]"):
        assert action in listener


@NODE
def test_j_and_k_walk_the_filtered_list_and_stop_at_both_ends():
    data = [_clip("01"), _clip("02", publish_status="published"), _clip("03")]
    out = _clips_eval("""(() => {
      clipsUi.filter = "à valider"; clipsUi.video = ""; clipsUi.channel = "";
      return { next: clipsNeighbor("v1/01", 1), prev: clipsNeighbor("v1/01", -1), last: clipsNeighbor("v1/03", 1),
               index: clipsDrawerIndex("v1/03") };
    })()""", data)
    assert out["next"] == "v1/03" and out["prev"] is None and out["last"] is None  # 02 (publié) est hors filtre
    assert out["index"] == [2, 2]


# --- T10 : l'URL suit le tiroir ------------------------------------------------------------------------------------

@NODE
def test_the_address_names_the_open_clip_and_the_video_filter():
    out = _clips_eval("""(() => {
      const at = (h) => { location.hash = h; return clipsHashTarget(); };
      return { both: at("#/clips/v%201/01-p2"), video: at("#/clips/v1"), bare: at("#/clips"), hv: (() => { location.hash = "#/clips/v1/02"; return clipsHashVideo(); })() };
    })()""", [])
    assert out["both"] == {"video": "v 1", "clip": "01-p2"}
    assert out["video"] == {"video": "v1", "clip": None}
    assert out["bare"] == {"video": "", "clip": None}
    assert out["hv"] == "v1"


def test_opening_a_clip_pushes_its_address_and_closing_restores_the_list_address():
    js = _static("screens", "clips.js")
    assert "history.pushState(null, \"\", clipsClipHash(" in js or 'history.pushState(null, "", clipsClipHash(' in js
    assert 'document.addEventListener("clipper:drawer-close"' in js
    assert "history.replaceState(null, \"\", clipsListHash())" in js or 'history.replaceState(null, "", clipsListHash())' in js


@NODE
def test_clip_and_list_addresses_are_encoded():
    out = _clips_eval("""(() => { clipsUi.video = ""; const a = clipsListHash(); clipsUi.video = "v 1"; return { a, b: clipsListHash(), c: clipsClipHash("v 1/01 p") }; })()""", [])
    assert out == {"a": "#/clips", "b": "#/clips/v%201", "c": "#/clips/v%201/01%20p"}


# --- ADR-a308 : rapprochement du DOM, tiroir de la coquille, aucune classe utilitaire --------------------------------

def test_clips_and_clip_sheet_render_by_morph_and_open_the_shell_drawer_never_the_legacy_panel():
    for name in ("clips.js", "clip.js"):
        js = _static("screens", name)
        assert "body.innerHTML" not in js, name
        assert "morph(body," in js, name
    clips = _static("screens", "clips.js")
    assert "drawer.open(" in clips and "openPanel(" not in clips and "closeLayer(" not in clips
    for legacy in ("btn-xs", "btn-ok", "btn-bad", "btn-primary", "btn-ghost", "icon-btn", "pill-dark", "class=\"phone\""):
        assert legacy not in clips, legacy
    assert 'classList.add("v3")' not in clips and 'classList.add("v3")' not in _static("screens", "clip.js")


def test_clips_rules_live_in_console_css():
    assert not (STATIC / "style.css").exists()
    v3 = _static("console.css")
    for selector in (".clips", ".clip-poster", ".scores", ".score", ".issue", ".sel-bar",
                     ".clip-sheet", ".sheet-row", ".parts"):
        assert selector in v3, selector
    # jamais une couleur de texte ni un fond en dur hors des jetons (hors surimpressions sur l'image)
    mobile = v3[v3.index("@media (max-width: 900px)", v3.index(".clips")):]
    assert ".scores { grid-template-columns: repeat(3, 1fr); }" in mobile
    assert "44px" in mobile


def test_clips_v3_css_never_links_an_external_resource():
    assert not re.search(r"(?i)https?://|@import|url\(", _static("console.css"))
