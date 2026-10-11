"""Veille et calendrier (TASK-9b2242a14269) : style par défaut `[veille] default_channel` et état « programmé ».

Aucun réseau ni navigateur ; JS évalué sous node (vm). Fixtures sans nom réel (ma_chaine, compte_alpha).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipper import veille, worker
from clipper.config import Config
from clipper.web import create_app

from tests.test_web_veille import proposal, put_day, today

STATIC = Path(__file__).resolve().parents[1] / "clipper" / "web" / "static"
NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")


def _config(tmp_path, **veille_table) -> Config:
    return Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                  _sections={"veille": {"enabled": True, **veille_table}})


def _preset(tmp_path, name="ma_chaine") -> None:
    path = tmp_path / "presets" / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[channel]\n", encoding="utf-8")


@pytest.fixture
def enqueued(monkeypatch):
    calls = []

    def fake_enqueue(url, channel, action, force_steps=None, *, short_clips=None, config=None):
        calls.append(channel)
        return {"id": "e1", "video_id": "v1", "url": url, "channel": channel, "action": action,
                "force_steps": [], "status": "waiting"}

    monkeypatch.setattr(worker, "enqueue", fake_enqueue)
    return calls


# --- (1) API -------------------------------------------------------------------------------------------------

def test_default_channel_is_empty_in_config_defaults():
    assert veille.CONFIG_DEFAULTS["default_channel"] == ""


def test_clip_without_channel_applies_the_default_style(tmp_path, isolated_cwd, enqueued):
    _preset(tmp_path)
    put_day(tmp_path, today())
    c = TestClient(create_app(config=_config(tmp_path, default_channel="ma_chaine")))
    assert c.post(f"/api/veille/{today()}/twitch:v1/clip", json={}).status_code == 202
    assert enqueued == ["ma_chaine"]


def test_clip_null_channel_gets_the_default_but_explicit_empty_means_no_style(tmp_path, isolated_cwd, enqueued):
    _preset(tmp_path)
    put_day(tmp_path, today(), proposals=[proposal("v1"), proposal("v2", rank=2)])
    c = TestClient(create_app(config=_config(tmp_path, default_channel="ma_chaine")))
    assert c.post(f"/api/veille/{today()}/twitch:v1/clip", json={"channel": None}).status_code == 202
    assert c.post(f"/api/veille/{today()}/twitch:v2/clip", json={"channel": ""}).status_code == 202
    assert enqueued == ["ma_chaine", None]


def test_clip_explicit_channel_wins_over_the_default(tmp_path, isolated_cwd, enqueued):
    _preset(tmp_path)
    _preset(tmp_path, "autre")
    put_day(tmp_path, today())
    c = TestClient(create_app(config=_config(tmp_path, default_channel="ma_chaine")))
    assert c.post(f"/api/veille/{today()}/twitch:v1/clip", json={"channel": "autre"}).status_code == 202
    assert enqueued == ["autre"]


def test_clip_without_default_keeps_no_style(tmp_path, isolated_cwd, enqueued):
    put_day(tmp_path, today())
    c = TestClient(create_app(config=_config(tmp_path)))
    assert c.post(f"/api/veille/{today()}/twitch:v1/clip", json={}).status_code == 202
    assert enqueued == [None]


def test_default_channel_matching_no_preset_is_a_visible_config_error_never_a_fallback(tmp_path, isolated_cwd, enqueued):
    _preset(tmp_path, "autre")
    put_day(tmp_path, today())
    c = TestClient(create_app(config=_config(tmp_path, default_channel="fantome")))
    resp = c.post(f"/api/veille/{today()}/twitch:v1/clip", json={})
    assert resp.status_code == 422 and "default_channel" in resp.json()["detail"] and "fantome" in resp.json()["detail"]
    assert enqueued == []
    view = c.get("/api/veille")
    assert view.status_code == 422 and "default_channel" in view.json()["detail"]


def test_get_veille_exposes_the_default_channel(tmp_path, isolated_cwd):
    _preset(tmp_path)
    put_day(tmp_path, today())
    c = TestClient(create_app(config=_config(tmp_path, default_channel="ma_chaine")))
    assert c.get("/api/veille").json()["settings"]["default_channel"] == "ma_chaine"


# --- (1) Écran Veille ------------------------------------------------------------------------------------------

_VEILLE_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ctx = { console, document: { addEventListener() {} }, esc, CLIPPER_TZ: "Europe/Paris", Screens: {}, store: {},
  fr: (n) => String(n), icon: (name) => `<svg data-icon="${name}"></svg>`, currentScreen: "veille", renderCurrent() {},
  updateCounts() {}, api: async () => ({}), emptyState: (i, t) => `<div>${t}</div>` };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
process.stdout.write(JSON.stringify(vm.runInContext(fs.readFileSync(process.argv[3], "utf8"), ctx)));
"""

_CAND = {"id": "twitch:v1", "source": "twitch", "video_id": "v1", "url": "https://www.twitch.tv/videos/v1", "title": "Titre",
         "channel_name": "streamer_a", "game_key": "jeu a", "game_name": "Jeu A", "signals": {}}


def _node(tmp_path, harness: str, src: str, expr: str):
    (tmp_path / "h.js").write_text(harness, encoding="utf-8")
    (tmp_path / "e.js").write_text(expr, encoding="utf-8")
    done = subprocess.run(["node", str(tmp_path / "h.js"), str(STATIC / "screens" / src), str(tmp_path / "e.js")],
                          capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@NODE
def test_proposal_style_select_is_prefilled_with_the_default_style(tmp_path):
    out = _node(tmp_path, _VEILLE_HARNESS, "veille.js", f"""(() => {{
      veilleUi.data = {{ settings: {{ default_channel: "ma_chaine" }} }};
      const p = {{ candidate_id: "twitch:v1", status: "proposed", rank: 1, reason: "r", candidate: {json.dumps(_CAND)} }};
      const withDefault = veilleProposal(p, null, ["autre", "ma_chaine"]);
      veilleUi.style["twitch:v1"] = "autre";
      return {{ withDefault, chosen: veilleProposal(p, null, ["autre", "ma_chaine"]) }};
    }})()""")
    assert 'value="ma_chaine" selected' in out["withDefault"] and 'value="autre" selected' not in out["withDefault"]
    assert 'value="autre" selected' in out["chosen"] and 'value="ma_chaine" selected' not in out["chosen"]


@NODE
def test_style_to_send_is_the_default_untouched_and_empty_when_sans_style_is_chosen(tmp_path):
    out = _node(tmp_path, _VEILLE_HARNESS, "veille.js", """(() => {
      veilleUi.data = { settings: { default_channel: "ma_chaine" } };
      veilleUi.style["b"] = "";
      veilleUi.style["c"] = "autre";
      const withDefault = { untouched: veilleStyleToSend("a"), sansStyle: veilleStyleToSend("b"), chosen: veilleStyleToSend("c") };
      veilleUi.data = { settings: { default_channel: "" } };
      return { ...withDefault, noDefault: veilleStyleToSend("a") };
    })()""")
    assert out == {"untouched": "ma_chaine", "sansStyle": "", "chosen": "autre", "noDefault": ""}


# --- (2) Calendrier : « programmé » tant que le créneau est dans le futur ------------------------------------

_PUB_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = vm.createContext({ console, JSON, Date, Number, Math, Array, Object, Set, String, Intl, encodeURIComponent, decodeURIComponent,
  setTimeout, clearTimeout, location: { hash: "" }, document: { addEventListener() {}, querySelector() { return null; } },
  window: { addEventListener() {} }, Screens: {}, currentScreen: "publish",
  esc: (s) => String(s == null ? "" : s), fr: (n) => String(n), icon: () => "", $: () => null, $$: () => [],
  emptyState: () => "", renderCurrent() {}, api: async () => ({}), toast() {}, toastError() {}, morph() {}, netGuard: async () => true,
  jsonBody: (m, b) => ({ method: m, body: JSON.stringify(b) }), parseHashtags: (s) => [], copyText() {},
  confirmDialog: async () => true, openDialog() { return null; }, drawer: { open() {}, close() {}, isOpen() { return false; } } });
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
process.stdout.write(JSON.stringify(vm.runInContext(fs.readFileSync(process.argv[3], "utf8"), ctx)));
"""


@NODE
def test_published_post_with_a_future_slot_is_shown_scheduled_not_published(tmp_path):
    base = {"video_id": "v1", "clip_id": "01", "publish_status": "published", "service": "tiktok", "account": "ab12cd",
            "post_id": "7000000000000000001", "tiktok_status": "published", "screen_title": "Titre"}
    future = {**base, "slot_at": "2026-10-12T10:00:00+00:00"}
    past = {**base, "slot_at": "2026-10-10T10:00:00+00:00"}
    no_post = {k: v for k, v in future.items() if k != "post_id"}
    now = int(datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    out = _node(tmp_path, _PUB_HARNESS, "publish.js", f"""(() => {{
      const now = {now};
      const cls = (c) => (calPostHtml(c, "", now).match(/class="cal-post ([a-z]+)/) || [])[1];
      return {{ future: pubPostState({json.dumps(future)}, now), past: pubPostState({json.dumps(past)}, now),
               noPost: pubPostState({json.dumps(no_post)}, now), html: cls({json.dumps(future)}), htmlPast: cls({json.dumps(past)}) }};
    }})()""")
    assert out == {"future": "scheduled", "past": "published", "noPost": "published", "html": "scheduled", "htmlPast": "published"}
