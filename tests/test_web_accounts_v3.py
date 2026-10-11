"""Écran Comptes v3 (TASK-05d7b35de9ca, ADR-a308, SPEC-7715 E10).

Table (connexion / prêt / créneaux / posts), au plus 3 boutons visibles par ligne (le reste dans un menu),
note de pause affichée une seule fois, édition en tiroir (drawer de ui.js), rendu par morph.

Aucun test ne touche le réseau ni un navigateur : `screens/accounts.js` est lu tel quel et exécuté sous node
(quand il est là) avec des doublures minimales de la coquille. Données fictives : `ma_chaine`, `compte_alpha`.
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

ACCOUNTS_JS = (STATIC / "screens" / "accounts.js").read_text(encoding="utf-8")

STUBS = r"""
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fr = (n) => String(n);
const fmtParis = (iso) => '15 oct. 2026, 18:00';
const icon = (n, cls) => `<svg class="i ${cls || ''}" data-name="${n}"></svg>`;
const jsonBody = (method, body) => ({ method, body: JSON.stringify(body) });
const toasts = [], calls = [];
const toast = (o) => toasts.push(o);
const toastError = () => {};
const api = (url, opts) => { calls.push({ url, body: opts && opts.body }); return Promise.resolve({}); };
const confirmDialog = async () => true;
const emptyState = (ic, title, text) => `<div class="empty"><h3>${title}</h3><p>${text}</p></div>`;
const painted = [];
const morph = (el, html) => { painted.push(html); };
const drawerCalls = [], panelCalls = [];
const drawer = { open(html) { drawerCalls.push(html); return {}; }, close() {} };
const openPanel = (cls, html) => { panelCalls.push({ cls, html }); };
const closeLayer = () => {};
const location = { hostname: '127.0.0.1', hash: '#/accounts' };
const document = { addEventListener() {} };
const navigator = {};
const window = { addEventListener() {} };
const Screens = {};
let currentScreen = 'accounts';
const renderCurrent = () => {};
const fakeEl = () => ({ onclick: null, onsubmit: null, value: '', checked: false, disabled: false, innerHTML: '', dataset: {},
  classList: { add() {}, remove() {}, contains() { return false; } }, addEventListener() {}, focus() {}, closest() { return null; } });
const $ = () => fakeEl(), $$ = () => [];
const setTimeout = () => 0, clearTimeout = () => {};
"""

ACCOUNTS = [
    {"id": "ab12cd", "label": "ma_chaine", "platform": "TikTok", "username": "compte_alpha", "notes": "",
     "has_password": True, "service": "tiktok", "login": {"state": "connected", "checked_at": "2026-10-15T16:00:00+00:00"},
     "ready_to_publish": True, "ready_blocked_reason": "", "ready_note": "", "paused_at": None, "r4_halt": False,
     "slots": [{"day": "mon", "time": "18:00"}], "timezone": "Europe/Paris", "posts_today": 1, "max_posts_per_day": 3,
     "last_failure": None, "browser": {"present": True, "modified_at": "2026-10-14T09:00:00+00:00", "error": None}},
    {"id": "ef34gh", "label": "compte_beta", "platform": "YouTube", "username": "", "notes": "",
     "has_password": False, "service": "youtube", "login": {"state": "expired"}, "login_error": "session expirée",
     "ready_to_publish": False, "ready_blocked_reason": "En pause (manuel) depuis le 15 oct. 2026, 18:00",
     "ready_note": "En pause (manuel) depuis le 15 oct. 2026, 18:00", "paused_at": "2026-10-15T17:00:00+00:00",
     "r4_halt": True, "slots": [], "timezone": "Europe/Paris", "posts_today": None, "max_posts_per_day": None,
     "publish_error": None, "last_failure": {"channel": "ma_chaine", "clip_id": "02", "reason": "captcha détecté",
     "capture_url": "/api/publish/x/02/capture"}, "browser": None},
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
    """Exécute stubs + accounts.js + le corps de test dans un même contexte ; le corps finit par console.log(JSON)."""
    script = STUBS + "\n" + ACCOUNTS_JS + "\n" + f"const ACCOUNTS_FIXTURE = {json.dumps(ACCOUNTS, ensure_ascii=False)};\n" + body
    return json.loads(_node(script))


# --- C1 : table ------------------------------------------------------------------------------------------------

@NODE
def test_accounts_screen_is_one_table_with_connexion_pret_creneaux_posts_columns():
    out = _run("""
      accUi.list = ACCOUNTS_FIXTURE;
      const section = { classList: { contains: () => false, add(c) { this.added = c; } } };
      Screens.accounts.render({ closest: () => section });
      const html = painted[painted.length - 1];
      const heads = [...html.matchAll(/<th[^>]*>([^<]*)<\\/th>/g)].map((m) => m[1].trim());
      console.log(JSON.stringify({ heads, rows: (html.match(/<tr[ >]/g) || []).length, table: html.includes('<table class="table acc-table">'),
                                   added: section.classList.added || null }));
    """)
    assert out["heads"] == ["Compte", "Connexion", "Prêt", "Créneaux", "Posts", ""], out["heads"]
    assert out["table"] is True and out["rows"] >= 3   # en-tête + 2 comptes
    assert out["added"] == "v3"                      # écran migré (ADR-a308 §6)


@NODE
def test_the_screen_is_rendered_by_morph_not_by_innerhtml():
    out = _run("""
      accUi.list = ACCOUNTS_FIXTURE;
      const body = { closest: () => null, innerHTML: 'avant' };
      painted.length = 0;
      Screens.accounts.render(body);
      console.log(JSON.stringify({ painted: painted.length, innerHTML: body.innerHTML }));
    """)
    assert out["painted"] == 1 and out["innerHTML"] == "avant"


# --- C2 : au plus 3 boutons visibles par ligne -----------------------------------------------------------------

@NODE
@pytest.mark.parametrize("index", [0, 1])
def test_each_row_shows_at_most_three_buttons_outside_the_menu(index):
    out = _run(f"""
      const html = accRow(ACCOUNTS_FIXTURE[{index}]);
      const outside = html.replace(/<div class="vmenu" role="menu"[^>]*>[\\s\\S]*?<\\/div>/g, '');
      console.log(JSON.stringify({{ visible: (outside.match(/<button[\\s>]/g) || []).length,
                                  menu: (html.match(/role="menuitem"/g) || []).length,
                                  labels: [...outside.matchAll(/<button[^>]*>([\\s\\S]*?)<\\/button>/g)].map((m) => m[1].replace(/<[^>]+>/g, '').trim()) }}));
    """)
    assert out["visible"] <= 3, out["labels"]
    assert out["menu"] >= 1          # le reste des actions est dans le menu « … »
    assert "Se connecter" in " ".join(out["labels"])


@NODE
def test_the_pause_note_is_shown_once_per_row():
    out = _run("""
      const html = accRow(ACCOUNTS_FIXTURE[1]);
      console.log(JSON.stringify({ count: html.split("En pause").length - 1 }));
    """)
    assert out["count"] == 1


# --- C4 : édition en tiroir -------------------------------------------------------------------------------------

@NODE
def test_editing_an_account_opens_the_shared_drawer_not_a_modal():
    out = _run("""
      accOpenForm(ACCOUNTS_FIXTURE[0]);
      console.log(JSON.stringify({ drawer: drawerCalls.length, panel: panelCalls.length,
                                   head: (drawerCalls[0] || '').includes('drawer-head'),
                                   foot: (drawerCalls[0] || '').includes('drawer-foot') }));
    """)
    assert out["drawer"] == 1 and out["panel"] == 0
    assert out["head"] and out["foot"]


def test_accounts_js_has_no_modal_and_no_innerhtml_assignment_in_render():
    js = ACCOUNTS_JS
    assert "openPanel(" not in js and "acc-modal" not in js
    render = js[js.index("Screens.accounts = {"):]
    assert "innerHTML =" not in render
    assert 'classList.add("v3")' in js


# --- C6 : CSS fusionné dans v3.css, accounts.css retiré --------------------------------------------------------

def test_accounts_css_is_merged_into_v3_css_and_its_sheet_is_gone():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    v3 = (STATIC / "v3.css").read_text(encoding="utf-8")
    assert "screens/accounts.css" not in page
    assert not (STATIC / "screens" / "accounts.css").exists()
    assert "Comptes v3" in v3
    assert ".v3 .acc-table" in v3 or ".v3 .acc-row" in v3
    assert re.search(r"(?m)^\.acc-modal\b", v3) is None


@NODE
def test_the_service_tag_is_not_repeated_as_platform_tag():
    out = _run("""
      const html = accRow(ACCOUNTS_FIXTURE[0]);   // plateforme « TikTok », service TikTok : une seule pastille
      console.log(JSON.stringify({ tags: (html.match(/<span class="tag"/g) || []).length }));
    """)
    assert out["tags"] == 1


@NODE
def test_posts_cell_shows_only_the_count_and_the_email_keeps_a_title():
    out = _run("""
      const html = accRow(ACCOUNTS_FIXTURE[0]);
      console.log(JSON.stringify({ label: html.includes('>Posts du jour'), count: html.includes('<b>1 / 3</b>'),
                                   title: html.includes('class="acc-line mono acc-ellipsis" title="compte_alpha"') }));
    """)
    assert out["label"] is False      # l'intitulé est déjà en en-tête de colonne
    assert out["count"] is True and out["title"] is True   # adresse coupée proprement, complète en infobulle
