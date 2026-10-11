"""Écran Publication v3 (TASK-e3dee736b676, ADR-a308, SPEC-7715 E6 / T6 / T10).

Aucun test ne touche le réseau ni un navigateur : publish.js est lu tel quel, évalué entier sous node dans un
contexte vm avec des bouchons de page. Fixtures sans nom réel (ma_chaine, compte_alpha).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "clipper" / "web" / "static"
NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")


def _static(*parts: str) -> str:
    return (STATIC / Path(*parts)).read_text(encoding="utf-8")


_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = vm.createContext({
  console, JSON, Date, Number, Math, Array, Object, Set, String, Intl, encodeURIComponent, decodeURIComponent, setTimeout, clearTimeout,
  location: { hash: "" }, document: { addEventListener() {}, querySelector() { return null; } }, window: { addEventListener() {} },
  Screens: {}, currentScreen: "publish",
  esc: (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  fr: (n, d) => Number(n).toLocaleString("fr-FR", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 }),
  icon: (n, cls) => `<svg class="i ${cls || ""}" data-name="${n}"></svg>`,
  $: () => null, $$: () => [], emptyState: (i, t, x) => `<div class="empty"><h3>${t}</h3><p>${x}</p></div>`,
  renderCurrent() {}, api: async () => ({}), toast() {}, toastError() {}, morph() {}, netGuard: async () => true,
  jsonBody: (m, b) => ({ method: m, body: JSON.stringify(b) }), parseHashtags: (s) => s.split(/\s+/).filter(Boolean),
  copyText() {}, confirmDialog: async () => true, openDialog() { return null; }, drawer: { open() {}, close() {}, isOpen() { return false; } },
});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const out = vm.runInContext(fs.readFileSync(process.argv[3], "utf8"), ctx);
process.stdout.write(JSON.stringify(out === undefined ? null : out));
"""


def _eval(expr: str):
    """Évalue `expr` dans publish.js chargé entier ; renvoie le résultat JSON."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "h.js").write_text(_HARNESS, encoding="utf-8")
        (root / "expr.js").write_text(expr, encoding="utf-8")
        done = subprocess.run(["node", str(root / "h.js"), str(STATIC / "screens" / "publish.js"), str(root / "expr.js")],
                              capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _post(clip_id="01", status="scheduled", **over) -> dict:
    base = {
        "video_id": "v1", "clip_id": clip_id, "publish_status": status, "screen_title": f"Titre {clip_id}", "service": "tiktok",
        "account": "ab12cd", "slot_at": "2026-10-08T10:00:00+00:00", "slot_at_paris": "2026-10-08T12:00:00+02:00",
        "video_url": "/m", "thumbnail_url": "/t",
    }
    base.update(over)
    return base


def _days(posts_by_date: dict[str, list[dict]]) -> list[dict]:
    out = []
    for i in range(7):
        ymd = (date(2026, 10, 5) + timedelta(days=i)).isoformat()
        posts = posts_by_date.get(ymd, [])
        out.append({"date": ymd, "count": len(posts), "posts": posts})
    return out


def _week(posts_by_date=None, **over) -> dict:
    base = {"range": "week", "range_start": "2026-10-05", "range_end": "2026-10-11", "week_start": "2026-10-05",
            "week_end": "2026-10-11", "slots": [], "unscheduled": [], "done": [], "off_slot": [],
            "days": _days(posts_by_date or {}), "accounts": [{"id": "ab12cd", "label": "compte_alpha", "ready_to_publish": True}],
            "reason": None}
    base.update(over)
    return base


# --- Calendrier : carte de publication par état --------------------------------------------------------------

@NODE
def test_cal_post_html_classes_the_post_by_state_published_scheduled_or_failed():
    out = _eval(f"""(() => {{
      const cls = (p) => (calPostHtml(p).match(/class="cal-post ([a-z]+)/) || [])[1];
      return {{
        published: cls({json.dumps(_post("01", "published"))}),
        scheduled: cls({json.dumps(_post("02", "scheduled"))}),
        approved: cls({json.dumps(_post("03", "approved"))}),
        failed: cls({json.dumps(_post("04", "failed"))}),
        notLive: cls({json.dumps(_post("05", "published", tiktok_status="scheduled_on_tiktok", tiktok_live=False))}),
        refused: cls({json.dumps(_post("06", "published", tiktok_status="refused_by_platform"))}),
      }};
    }})()""")
    assert out == {"published": "published", "scheduled": "scheduled", "approved": "scheduled", "failed": "failed",
                   "notLive": "scheduled", "refused": "failed"}


@NODE
def test_cal_post_html_shows_time_title_account_and_only_movable_posts_are_draggable():
    out = _eval(f"""({{
      movable: calPostHtml({json.dumps(_post("01", "scheduled"))}),
      done: calPostHtml({json.dumps(_post("02", "published"))}),
      verify: calPostHtml({json.dumps(_post("03", "failed", to_verify=True))}),
    }})""")
    html = out["movable"]
    assert 'data-post="v1/01"' in html and 'draggable="true"' in html and 'tabindex="0"' in html
    assert "12:00" in html and "Titre 01" in html and "data-post-account" in html and "TikTok" in html
    assert 'draggable="false"' in out["done"] and 'draggable="false"' in out["verify"]


# --- Agenda mobile (SPEC-7715 T6) ---------------------------------------------------------------------------

@NODE
def test_agenda_html_lists_each_day_with_its_posts_and_says_when_a_day_is_empty():
    week = _week({"2026-10-06": [_post("01", "published"), _post("02", "failed")]})
    html = _eval(f"agendaHtml({json.dumps(week['days'])})")
    assert html.count("data-agenda-day=") == 7
    day = html[html.index('data-agenda-day="2026-10-06"'):html.index('data-agenda-day="2026-10-07"')]
    assert "Titre 01" in day and "Titre 02" in day and day.count("data-post=") == 2 and "12:00" in day
    assert "Publié" in day and "Échec" in day
    assert "Rien ce jour" in html[html.index('data-agenda-day="2026-10-07"'):]


@NODE
def test_the_calendar_zone_carries_both_the_grid_and_the_agenda_for_the_css_to_choose():
    html = _eval(f"""(() => {{ pubUi.range = 'week'; return pubCalendarZone({json.dumps(_week({"2026-10-06": [_post("01")]}))}); }})()""")
    assert 'id="pub-cal"' in html and 'class="agenda"' in html and html.index('id="pub-cal"') < html.index('class="agenda"')


def test_agenda_replaces_the_calendar_grid_under_900_px_in_v3_css():
    css = _static("v3.css")
    assert re.search(r"\.v3 \.agenda\s*\{[^}]*display:\s*none", css)
    media = css[css.index("@media (max-width: 900px)", css.index(".v3 .agenda")):]
    media = media[:media.index("\n}\n")]
    assert re.search(r"\.v3 \.cal-wrap\s*\{[^}]*display:\s*none", media) and re.search(r"\.v3 \.agenda\s*\{[^}]*display:\s*block", media)


# --- Rail « À faire » ----------------------------------------------------------------------------------------

def _plan(**over) -> dict:
    base = {"day": "2026-10-12", "enabled": True, "status": "absent", "accounts": [], "notes": []}
    base.update(over)
    return base


@NODE
@pytest.mark.parametrize("plan,busy,reason", [
    (_plan(status="absent"), None, "calcule-le d'abord"),
    (_plan(status="validated", validated_at="2026-10-11T20:00:00+00:00"), None, "déjà validé"),
    (_plan(status="error", error={"message": "boom"}), None, "erreur"),
    (_plan(status="proposed", accounts=[{"account": "ab12cd", "label": "compte_alpha", "lines": [{"slot_at": "2026-10-12T10:00:00+00:00", "refusal": "plafond"}]}]), None, "refusées"),
    (_plan(status="proposed", accounts=[{"account": "ab12cd", "label": "compte_alpha", "lines": [{"slot_at": "2026-10-12T10:00:00+00:00"}]}]), "compute", "en cours"),
])
def test_validate_plan_button_is_greyed_with_the_reason_in_its_title(plan, busy, reason):
    html = _eval(f"""(() => {{ pubRep.data = {json.dumps(plan)}; pubRep.busy = {json.dumps(busy)}; return repHtml(); }})()""")
    button = re.search(r"<button[^>]*data-rep-validate[^>]*>", html).group(0)
    assert " disabled" in button
    title = re.search(r'title="([^"]*)"', button)
    assert title and reason in title.group(1), button


@NODE
def test_validate_plan_button_is_enabled_without_a_reason_when_a_line_is_accepted():
    plan = _plan(status="proposed", accounts=[{"account": "ab12cd", "label": "compte_alpha",
                                                "lines": [{"slot_at": "2026-10-12T10:00:00+00:00", "video_id": "v1", "clip_id": "01"}]}])
    html = _eval(f"(() => {{ pubRep.data = {json.dumps(plan)}; pubRep.busy = null; return repHtml(); }})()")
    button = re.search(r"<button[^>]*data-rep-validate[^>]*>", html).group(0)
    assert " disabled" not in button and "title=" not in button


@NODE
def test_the_layout_has_no_help_banner_but_an_info_tooltip_and_the_calendar_comes_first():
    html = _eval(f"pubLayoutHtml({json.dumps(_week())}, pubPostsSection())")
    assert 'class="banner"' not in html and "<b>Publier :</b>" not in html
    help_button = re.search(r"<[^>]*data-pub-help[^>]*>", html).group(0)
    assert 'title="' in help_button and "Nouvelle publication" in help_button and "facultatifs" in help_button
    assert html.index("data-pub-new") < html.index('id="pub-cal"') < html.index("data-pub-rail")
    rail = html[html.index("data-pub-rail"):]
    assert "À faire" in rail and rail.index("À faire") < rail.index("Plan de demain") or "Plan de demain" not in rail


@NODE
def test_failed_posts_are_listed_apart_in_the_rail_with_capture_and_retry():
    out = _eval(f"""(() => {{
      pubUi.account = '';
      pubPosts.data = {{ accounts: [], publications: [
        {json.dumps(_post("01", "approved", editable=True, publish_mode="immediate"))},
        {json.dumps(_post("02", "failed", editable=True, publish_error="captcha", capture_url="/media/cap.png"))}],
        refused_by_platform: [], removed_from_platform: [] }};
      return pubPostsSection();
    }})()""")
    failed = out[out.index("En échec"):]
    assert "Titre 02" in failed and "captcha" in failed and "voir la capture" in failed and "data-pub-retry" in failed
    assert "Titre 02" not in out[:out.index("En échec")]


# --- Source : coquille v3, rapprochement, dialogues natifs ----------------------------------------------------

def test_publish_screen_is_marked_v3_and_paints_with_morph_not_inner_html():
    js = _static("screens", "publish.js")
    view = js[js.index("function pubView("):js.index("/* ---------- Plan de demain")]
    assert "morph(body, html)" in view and "innerHTML" not in view
    render = js[js.index("Screens.publish = {"):]
    assert 'classList.add("v3")' in render


def test_new_publication_and_series_use_the_native_dialog_never_the_legacy_panel():
    js = _static("screens", "publish.js")
    assert "openPanel(" not in js and "closeLayer(" not in js
    for name in ("pubOpenForm", "pubOpenSeriesForm"):
        start = js.index(f"async function {name}")
        assert "openDialog(" in js[start:js.index("\n}\n", start)], name


def test_publish_blocks_left_style_css_for_v3_css():
    css = _static("style.css")
    assert "/* >>> publish */" not in css
    for selector in (".cal", ".cal-h", ".cal-c", ".post", ".cal-day", ".pub-grid", ".pub-toolbar", ".rep-plan", ".pubf-clips"):
        assert not re.search(rf"^{re.escape(selector)}\b", css, re.M), selector
    v3 = _static("v3.css")
    for selector in (".v3 .cal", ".v3 .cal-post", ".v3 .cal-day", ".v3 .agenda", ".v3 .pub-rail", ".v3 .pubf-clips"):
        assert selector in v3, selector


def test_publish_v3_css_never_links_an_external_resource():
    assert not re.search(r"(?i)https?://|@import|url\(", _static("v3.css"))
