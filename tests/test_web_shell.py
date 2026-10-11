"""Coquille v3 de la console (TASK-0b013cbf1bdd, ADR-a308, SPEC-7715).

Aucun test ne touche le réseau ni un navigateur : les fichiers statiques sont lus tels quels,
la logique pure (thème, toasts, palette, raccourcis, morph) tourne sous node quand il est là.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "clipper" / "web" / "static"
NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")

SCREENS = ("dashboard", "veille", "videos", "review", "clips", "clip", "channels", "publish", "stats",
           "accounts", "settings", "journal")
# Écrans qui ont (encore) un bloc dans style.css : chaque tâche d'écran retire le sien entre ses délimiteurs.
# U5 (TASK-1679e58d021b) a retire les blocs « clips » et « clip » : leurs regles vivent dans v3.css.
STYLE_BLOCKS = ("dashboard", "videos", "review", "channels", "publish", "stats", "settings",
                "journal", "shared", "shell")


def _static(*parts: str) -> str:
    return (STATIC / Path(*parts)).read_text(encoding="utf-8")


# --- aides node ---------------------------------------------------------------------------------------

def _js_def(source: str, name: str) -> str:
    """Texte de `function name(...) {...}` (fin au premier `}` en colonne 0) ou de `const name = ...;`."""
    for marker in (f"function {name}(", f"async function {name}(", f"const {name} = "):
        start = source.find(marker)
        if start >= 0:
            break
    else:
        raise AssertionError(f"{name} introuvable")
    if "function" in marker:
        return source[start:source.index("\n}\n", start) + 3]
    end = start
    while True:
        end = source.index(";\n", end) + 2
        chunk = source[start:end]
        if chunk.count("(") == chunk.count(")") and chunk.count("{") == chunk.count("}") and chunk.count("`") % 2 == 0:
            return chunk


_PRELUDE = (
    "const esc = (s) => String(s == null ? '' : s).replace(/[&<>\"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));\n"
    "const icon = (n, cls) => `<svg class=\"i ${cls || ''}\" data-name=\"${n}\"></svg>`;\n"
)


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


def _run(defs, expr: str, preamble: str = ""):
    parts = [_PRELUDE, preamble]
    for relative, names in defs:
        source = _static(relative)
        parts += [_js_def(source, n) for n in names]
    return json.loads(_node("\n".join(parts) + f"\nconsole.log(JSON.stringify({expr}));"))


# --- index.html : squelette intact, coquille v3 ----------------------------------------------------------

def test_index_keeps_nav_tabbar_screen_sections_and_script_order():
    page = _static("index.html")
    assert re.search(r'<nav class="nav"[^>]*id="nav"', page)
    assert re.search(r'<nav class="tabbar[^"]*"[^>]*id="tabbar"', page)
    for screen in SCREENS:
        assert f'<section class="screen" id="screen-{screen}"' in page, screen  # aucun écran migré par U2
    order = ["/static/vendor/idiomorph.min.js", "/static/vendor/basecoat.all.min.js", "/static/icons.js",
             "/static/ui.js", "/static/screens.js", "/static/screens/jury-radar.js", "/static/screens/videos.js",
             "/static/screens/dashboard.js", "/static/screens/review.js", "/static/screens/clips.js",
             "/static/screens/clip.js", "/static/screens/channels.js", "/static/screens/layout.js",
             "/static/screens/layout-letterbox.js", "/static/screens/chan-subtitles-preview.js",
             "/static/screens/watch.js", "/static/screens/veille.js", "/static/screens/publish.js",
             "/static/screens/settings.js", "/static/screens/stats.js", "/static/screens/accounts.js",
             "/static/screens/journal.js", "/static/app.js"]
    positions = [page.index(f'src="{src}"') for src in order]
    assert positions == sorted(positions)
    # feuilles : couche « legacy » déclarée avant Basecoat (ses @layer la dominent alors), style.css et les feuilles
    # d'écran importées dans cette couche, v3.css hors couche en dernier
    sheets = ["<style>@layer properties, theme, base, legacy, components, utilities;</style>", 'href="/static/vendor/basecoat.cdn.min.css"',
              '@import url("/static/style.css") layer(legacy);', '@import url("/static/screens/accounts.css") layer(legacy);',
              '@import url("/static/screens/veille.css") layer(legacy);', 'href="/static/v3.css"']
    offsets = [page.index(needle) for needle in sheets]
    assert offsets == sorted(offsets)
    assert 'href="/static/style.css"' not in page  # jamais chargée deux fois (lien + import)


class _Tree(HTMLParser):
    """Arbre minimal : pour chaque balise ouvrante, la pile des classes de ses ancêtres."""

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, str]] = []
        self.found: list[tuple[str, str, list[str]]] = []  # (tag, class, ancêtres=classes)

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "") or ""
        self.found.append((tag, cls, [c for _, c in self.stack]))
        if tag not in ("meta", "link", "img", "input", "br", "hr"):
            self.stack.append((tag, cls))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def test_shell_pieces_carry_v3_but_never_an_ancestor_of_a_screen():
    tree = _Tree()
    tree.feed(_static("index.html"))
    by_tag = {}
    for tag, cls, ancestors in tree.found:
        by_tag.setdefault((tag, cls), ancestors)
    v3 = lambda cls: "v3" in cls.split()
    assert any(tag == "aside" and v3(cls) for tag, cls, _ in tree.found), "barre latérale sans v3"
    assert any(tag == "header" and "topbar" in cls and v3(cls) for tag, cls, _ in tree.found)
    assert any(tag == "nav" and "tabbar" in cls and v3(cls) for tag, cls, _ in tree.found)
    for tag, cls, ancestors in tree.found:
        if tag == "section" and "screen" in cls.split():
            assert not v3(cls), "U2 ne migre aucun écran"
            assert not any(v3(a) for a in ancestors), f"un ancêtre de #{cls} porte v3 : Basecoat restylerait l'écran"
    # dialogues, toaster et tiroir : sous un conteneur .v3 (un <dialog class="v3"> ne reçoit pas .v3 .dialog)
    for tag, cls, ancestors in tree.found:
        if tag == "dialog" or "toaster" in cls.split() or "drawer-panel" in cls.split():
            assert any(v3(a) for a in ancestors), f"<{tag} class={cls!r}> sans ancêtre .v3"
    assert any(tag == "aside" and "sidebar" in cls.split() for tag, cls, _ in tree.found) is False, \
        "basecoat.all.min.js attache son composant à .sidebar (inert sous 768 px) : la barre v3 s'appelle autrement"


def test_index_links_the_vendored_files_and_the_v3_sheet_without_external_resource():
    page = _static("index.html")
    for href in ("/static/vendor/basecoat.cdn.min.css", "/static/v3.css"):
        assert f'href="{href}"' in page
    css = _static("v3.css")
    assert not re.search(r"(?i)https?://|@import|url\(\s*['\"]?//", css)
    assert 'id="toaster"' in page and 'id="palette"' in page and 'id="shortcuts"' in page and 'id="drawer"' in page
    assert 'id="btn-palette"' in page and 'id="btn-rail"' in page


# --- thème : clair par défaut, sombre mémorisé ----------------------------------------------------------

@NODE
def test_choose_theme_is_light_unless_dark_was_remembered():
    out = _run([("app.js", ["chooseTheme"])], 'chooseTheme(null) + " " + chooseTheme("dark") + " " + chooseTheme("light") + " " + chooseTheme("auto")')
    assert out == "light dark light light"


@NODE
def test_inline_head_script_applies_light_theme_when_storage_is_empty_and_never_asks_the_system():
    page = _static("index.html")
    inline = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    assert "prefers-color-scheme" not in inline and "matchMedia" not in inline
    stub = """
    const calls = [];
    const classes = new Set();
    globalThis.localStorage = { getItem: (k) => STORE[k] === undefined ? null : STORE[k] };
    globalThis.document = { documentElement: { dataset: {}, classList: { toggle: (c, on) => { on ? classes.add(c) : classes.delete(c); } } } };
    globalThis.window = globalThis;
    """
    for store, theme, dark in (("{}", "light", False), ('{"clipper-theme": "dark"}', "dark", True),
                               ('{"clipper-theme": "bizarre"}', "light", False)):
        out = _node(f"const STORE = {store};\n" + stub + inline
                    + "\nconsole.log(JSON.stringify([document.documentElement.dataset.theme, classes.has('dark'), document.documentElement.dataset.nav || null]));")
        assert json.loads(out) == [theme, dark, None], store
    out = _node('const STORE = {"clipper-nav": "rail"};\n' + stub + inline
                + "\nconsole.log(JSON.stringify(document.documentElement.dataset.nav));")
    assert json.loads(out) == "rail"


def test_apply_theme_sets_both_the_dark_class_and_the_legacy_data_theme():
    app = _static("app.js")
    body = _js_def(app, "applyTheme")
    assert 'classList.toggle("dark"' in body and "dataset.theme = theme" in body
    assert "prefers-color-scheme" not in app and "followSystemTheme" not in app
    assert 'THEME_KEY = "clipper-theme"' in app
    assert '<html lang="fr" data-theme="light">' in _static("index.html")
    assert 'name="theme-color" content="#f6f4ef"' in _static("index.html")


# --- toasts Basecoat, API toast() conservée -----------------------------------------------------------------

@NODE
def test_toast_with_undo_renders_an_annuler_button_and_without_undo_none():
    defs = [("ui.js", ["TOAST_CATEGORY", "TOAST_ICON", "toastHtml"])]
    with_undo = _run(defs, "toastHtml({ kind: 'ok', title: 'Retirée', body: 'abc', undo: () => 1, action: null, ms: 5000 })")
    assert "data-toast-cancel" in with_undo and ">Annuler<" in with_undo
    assert 'data-category="success"' in with_undo and 'role="status"' in with_undo
    assert "<h2>Retirée</h2>" in with_undo and "<p>abc</p>" in with_undo
    assert 'data-toast-initialized="true"' in with_undo  # Basecoat ne double pas la minuterie
    without = _run(defs, "toastHtml({ kind: 'bad', title: 'Échec', body: '', undo: null, action: { label: 'Voir', run: () => 1 }, ms: 7000 })")
    assert "Annuler" not in without and ">Voir<" in without and "data-toast-action" in without
    assert 'role="alert"' in without and 'data-category="error"' in without
    escaped = _run(defs, "toastHtml({ kind: 'info', title: '<b>x</b>', body: '', undo: null, action: null, ms: 1 })")
    assert "<b>x</b>" not in escaped and "&lt;b&gt;" in escaped


def test_toast_function_appends_to_the_basecoat_toaster_and_keeps_its_contract():
    ui = _static("ui.js")
    body = _js_def(ui, "toast")
    assert "toastHtml(" in body and '"#toaster"' in body
    assert "UNDO_MS = 5000" in ui and "if (o.undo) o.ms = UNDO_MS" in body
    assert "[data-toast-cancel]" in body and "[data-toast-action]" in body
    assert "mouseenter" in body and "mouseleave" in body            # survol : la minuterie attend
    assert 'setAttribute("aria-hidden", "true")' in body             # sortie Basecoat
    assert "#toasts" not in ui and "#toasts" not in _static("app.js")


# --- rapprochement du DOM : morph(), plus d'innerHTML dans app.js ----------------------------------------------

def test_render_current_and_app_js_never_assign_inner_html():
    app = _static("app.js")
    assert "innerHTML" not in _js_def(app, "renderCurrent")
    assert not re.search(r"\.innerHTML\s*=", app), "app.js passe par morph() (Idiomorph), jamais innerHTML ="
    assert "morph(" in app
    assert "function guardBodyHtml(" in app  # écrans non migrés : garde conservée


@NODE
def test_morph_refuses_to_run_without_idiomorph_and_morphs_inner_html_with_it():
    defs = [("ui.js", ["morph"])]
    out = _run(defs, """(() => {
      let err = null;
      try { morph({}, '<p>x</p>'); } catch (e) { err = e.message; }
      const calls = [];
      globalThis.Idiomorph = { morph: (el, html, opts) => calls.push([html, opts]) };
      morph({ id: 'body' }, '<p>y</p>');
      return { err, calls };
    })()""")
    assert out["err"] and "Idiomorph" in out["err"]
    assert out["calls"] == [["<p>y</p>", {"morphStyle": "innerHTML"}]]


# --- dialogues natifs : confirmDialog et openDialog ---------------------------------------------------------------

def test_confirm_dialog_is_a_native_dialog_opened_in_the_v3_layer():
    ui = _static("ui.js")
    confirm = _js_def(ui, "confirmDialog")
    assert "openDialog(" in confirm and "openPanel(" not in confirm
    assert "Annuler" in confirm and "data-confirm" in confirm and 'data-variant="destructive"' in confirm
    opener = _js_def(ui, "openDialog")
    assert 'createElement("dialog")' in opener and "showModal()" in opener and '"#layers"' in opener
    assert 'className = "dialog"' in opener
    assert 'addEventListener("close"' in opener and "remove()" in opener      # retiré du DOM après fermeture
    assert "e.target === dlg" in opener                                        # clic sur le fond = fermer
    # l'ancien openPanel reste pour les écrans non migrés et échappe au composant drawer de Basecoat
    legacy = _js_def(ui, "openPanel")
    assert 'setAttribute("role", "dialog")' in legacy and "drawerInitialized" in legacy


def test_drawer_api_exists_with_escape_and_scrim_closing():
    ui = _static("ui.js")
    drawer = _js_def(ui, "drawer")
    assert "open(" in drawer and "close(" in drawer and "isOpen(" in drawer
    assert "morph(" in drawer and '"#drawer"' in drawer and "#drawer-scrim" in drawer
    assert "clipper:drawer-close" in drawer
    app = _static("app.js")
    assert 'e.key === "Escape"' in app and "drawer.isOpen()" in app


# --- palette Ctrl K et raccourcis ---------------------------------------------------------------------------------

_PALETTE_DEFS = [("screens.js", ["STATUS_LABELS"]),
                 ("app.js", ["PALETTE_SCREENS", "PALETTE_ACTIONS", "paletteNorm", "paletteItems"])]
_STORE = json.dumps({"videos": [
    {"video_id": "abcdefghijk", "title": "Grosse soirée ranked", "status": "done"},
    {"video_id": "zzzzzzzzzzz", "title": "Un autre stream", "status": "failed"},
]})


@NODE
def test_palette_without_query_lists_screens_and_actions_but_no_video():
    items = _run(_PALETTE_DEFS, f"paletteItems('', {_STORE})")
    groups = {i["group"] for i in items}
    assert groups == {"Aller à", "Actions"}
    labels = [i["label"] for i in items]
    for label in ("Tableau de bord", "Vidéos", "Clips", "Publication", "Statistiques", "Styles", "Journal"):
        assert label in labels, label
    assert "Ajouter une vidéo" in labels and "Nouvelle publication" in labels and "Changer de thème" in labels
    screen = next(i for i in items if i["label"] == "Vidéos")
    assert screen["action"] == {"kind": "screen", "hash": "#/videos"} and screen["kbd"] == "g v"
    styles = next(i for i in items if i["label"] == "Styles")
    assert styles["action"]["hash"] == "#/styles"


@NODE
def test_palette_finds_a_video_by_title_or_by_id_and_filters_screens_without_accents():
    by_title = _run(_PALETTE_DEFS, f"paletteItems('soiree', {_STORE})")
    assert [i["label"] for i in by_title if i["group"] == "Vidéos"] == ["Grosse soirée ranked"]
    assert by_title[-1]["action"] == {"kind": "video", "id": "abcdefghijk"}
    assert "abcdefghijk" in by_title[-1]["sub"] and "terminée" in by_title[-1]["sub"]
    by_id = _run(_PALETTE_DEFS, f"paletteItems('zzzz', {_STORE})")
    assert [i["action"]["id"] for i in by_id if i["group"] == "Vidéos"] == ["zzzzzzzzzzz"]
    screens = _run(_PALETTE_DEFS, f"paletteItems('video', {_STORE})")
    assert "Vidéos" in [i["label"] for i in screens if i["group"] == "Aller à"]
    action = _run(_PALETTE_DEFS, f"paletteItems('ajout', {_STORE})")
    assert [i["label"] for i in action] == ["Ajouter une vidéo"]
    assert action[0]["action"] == {"kind": "action", "id": "add-video"}
    assert _run(_PALETTE_DEFS, f"paletteItems('xyzxyz', {_STORE})") == []
    assert _run(_PALETTE_DEFS, "paletteItems('abc', {})") == []  # magasin vide : rien n'explose


@NODE
def test_shortcuts_g_then_letter_slash_n_and_question_mark_outside_inputs():
    defs = [("app.js", ["GOTO_KEYS", "shortcutAction"])]
    out = _run(defs, """[
      shortcutAction('g', null, false), shortcutAction('d', 'g', false), shortcutAction('v', 'g', false),
      shortcutAction('c', 'g', false), shortcutAction('p', 'g', false), shortcutAction('s', 'g', false),
      shortcutAction('x', 'g', false), shortcutAction('n', null, false), shortcutAction('/', null, false),
      shortcutAction('?', null, false), shortcutAction('a', null, false), shortcutAction('n', null, true),
    ]""")
    assert out == [{"kind": "chord"}, {"kind": "goto", "hash": "#/dashboard"}, {"kind": "goto", "hash": "#/videos"},
                   {"kind": "goto", "hash": "#/clips"}, {"kind": "goto", "hash": "#/publish"},
                   {"kind": "goto", "hash": "#/stats"}, {"kind": "none"}, {"kind": "add"}, {"kind": "search"},
                   {"kind": "help"}, None, None]


def test_palette_is_wired_to_ctrl_k_the_topbar_button_and_renders_by_morph():
    app = _static("app.js")
    assert 'e.key.toLowerCase() === "k"' in app and "openPalette()" in app
    assert '"#btn-palette"' in app
    render = _js_def(app, "paletteRender")
    assert "morph(" in render and "innerHTML" not in render
    assert 'role="option"' in render and "aria-selected" in render
    page = _static("index.html")
    assert "Raccourcis clavier" in page and "Ctrl" in page and 'id="palette-q"' in page


# --- v3.css : jetons scopés, mobile, rail ------------------------------------------------------------------------

def test_v3_css_has_touch_targets_mobile_breakpoint_rail_and_scoped_tokens():
    css = _static("v3.css")
    assert "44px" in css and "@media (max-width: 900px)" in css
    assert 'html[data-nav="rail"]' in css
    assert "prefers-reduced-motion" in css
    # jetons v3 posés sur .v3, jamais sur :root (les :root de v3.css ne portent que les jetons legacy restaurés,
    # vérifiés par test_basecoat_unlayered_tokens_that_collide_with_style_css_are_restored_by_v3_css)
    root_blocks = re.findall(r"(?m)^:root[^{]*\{([^}]*)\}", css)
    for block in root_blocks:
        for name in ("--background:", "--foreground:", "--primary:", "--sidebar:", "--border:", "--card:"):
            assert name not in block, name
    assert re.search(r"(?m)^\.v3\s*\{[^}]*--background:\s*#f6f4ef", css)
    assert re.search(r"(?m)^\.dark \.v3\s*\{[^}]*--background:\s*#121210", css)
    assert "--color-background: var(--background)" in css   # Basecoat lit --color-*
    assert "--primary: #c25f00" in css and "--primary: #ff8a00" in css
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert stripped.count("{") == stripped.count("}")
    assert len(css.encode("utf-8")) <= 40 * 1024  # objectif ADR-a308


def test_v3_css_styles_the_dialog_and_its_backdrop_under_the_v3_container():
    css = _static("v3.css")
    assert ".v3 .dialog > :is(div, form) {" in css
    assert ".v3 .dialog::backdrop" in css
    assert ".v3 .toaster" in css and ".drawer-panel" in css and ".palette" in css


# --- style.css : délimiteurs par écran, coquille retirée ----------------------------------------------------------

def test_style_css_has_balanced_delimiters_for_every_screen_and_lost_its_shell_blocks():
    css = _static("style.css")
    opens = re.findall(r"/\* >>> ([a-z-]+) \*/", css)
    closes = re.findall(r"/\* <<< ([a-z-]+) \*/", css)
    assert opens and sorted(opens) == sorted(closes)
    assert set(opens) <= set(STYLE_BLOCKS), set(opens) - set(STYLE_BLOCKS)
    for name in ("dashboard", "videos", "review", "channels", "publish", "stats", "settings", "journal"):
        assert name in opens, f"aucun bloc /* >>> {name} */"
    assert "clips" not in opens and "clip" not in opens  # ecrans migres par U5 : plus de bloc legacy
    # un délimiteur ouvert est fermé avant le suivant du même nom, et les blocs ne se chevauchent pas
    depth = None
    for kind, name in re.findall(r"/\* (>>>|<<<) ([a-z-]+) \*/", css):
        if kind == ">>>":
            assert depth is None, f"bloc {depth} encore ouvert quand {name} s'ouvre"
            depth = name
        else:
            assert depth == name, f"fermeture de {name} sans ouverture"
            depth = None
    assert depth is None
    for gone in ("\n.sidebar {", "\n.toasts {", "\n.toast {", "\n.tabbar {", "\n.topbar {", "\n.net-pill {",
                 "\n.conn-banner {", "\n.nav a {", "\n.brand {", "\n.machine {", "\n.palette {", "\n.popover {"):
        assert gone not in css, f"{gone.strip()} appartient à la coquille v3 (v3.css)"
    assert "--topbar: 52px" in css  # hauteur de la barre v3, pour les offsets sticky des écrans


def test_screen_content_rules_are_untouched_in_style_css():
    """Les écrans non migrés ne changent pas d'un pixel : leurs blocs restent, avec leurs valeurs."""
    css = _static("style.css")
    for needle in (".view { flex: 1; padding: 32px; max-width: 1480px;", ".panel { background: var(--surface);",
                   ".btn {\n  display: inline-flex;", ".modal {\n  position: fixed; z-index: 61;",
                   ".overlay { position: fixed; inset: 0; z-index: 60;",  # .drawer legacy retiré par U5 : seul Clips l'ouvrait
                   ".skeleton { border-radius: var(--r);", ".empty { padding: 48px 24px;",
                   ".clip { display: flex; flex-direction: column; gap: 8px; cursor: pointer; }",  # carte de la Veille
                   ".hashtags { display: flex; flex-wrap: wrap; gap: 6px; }",  # Publication
                   ".token-view {", ".screen[hidden] { display: none; }", ':root[data-theme="light"] {'):
        assert needle in css, needle


# --- garde : les écrans non migrés ne changent pas d'un pixel (cascade Basecoat / legacy) --------------------------

def _css_top_level(css: str):
    """Règles de premier niveau d'une feuille : liste de (prélude, corps) ; les @layer gardent leur corps entier."""
    out, depth, start, prelude, in_str, i = [], 0, 0, "", None, 0
    while i < len(css):
        c = css[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        elif c in "\"'":
            in_str = c
        elif c == "{":
            if depth == 0:
                prelude, body_start = css[start:i].strip(), i + 1
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                out.append((prelude, css[body_start:i]))
                start = i + 1
        elif c == ";" and depth == 0:
            out.append((css[start:i].strip(), None))
            start = i + 1
        i += 1
    return out


def _decls(body: str) -> dict:
    return {k.strip(): v.strip() for k, v in (d.split(":", 1) for d in body.split(";") if ":" in d)}


def test_legacy_layer_sits_between_basecoat_base_and_components():
    """Basecoat (Tailwind v4) est en couches : ses jetons :root/.dark hors couche, theme/base/components en @layer.
    style.css doit battre theme/base (ses --font-mono, --ease-out portent les mêmes noms) et perdre contre
    components (`.v3 .btn` dans la coquille) : la couche legacy est déclarée entre les deux, avant toute feuille."""
    page = _static("index.html")
    vendor = _static("vendor", "basecoat.cdn.min.css")
    layers = [p.split(None, 1)[1] for p, body in _css_top_level(vendor) if p.startswith("@layer") and body is not None]
    assert "theme" in layers and "base" in layers and "components" in layers
    stmt = re.search(r"<style>@layer ([a-z, ]+);</style>", page).group(1)
    order = [x.strip() for x in stmt.split(",")]
    assert order.index("base") < order.index("legacy") < order.index("components")
    for name in layers:
        assert name in order, f"couche Basecoat {name} absente de la déclaration d'ordre"
    assert page.index("<style>@layer ") < page.index('href="/static/vendor/basecoat.cdn.min.css"')
    for sheet in ("/static/style.css", "/static/screens/accounts.css", "/static/screens/veille.css"):
        assert f'@import url("{sheet}") layer(legacy);' in page, sheet


def test_basecoat_unlayered_tokens_that_collide_with_style_css_are_restored_by_v3_css():
    """Les :root / .dark de Basecoat sont hors couche, donc au-dessus de la couche legacy : chaque nom qu'ils
    partagent avec les :root de style.css est redéclaré hors couche dans v3.css, avec la valeur de style.css."""
    vendor = _static("vendor", "basecoat.cdn.min.css")
    basecoat = {}
    for prelude, body in _css_top_level(vendor):
        if prelude in (":root", ".dark") and body is not None:
            basecoat.setdefault(prelude, {}).update(_decls(body))
    assert basecoat[":root"] and basecoat[".dark"]
    legacy = {}
    for prelude, body in _css_top_level(re.sub(r"/\*.*?\*/", "", _static("style.css"), flags=re.S)):
        if prelude.startswith(":root") and body is not None:
            legacy[prelude] = _decls(body)
    dark, light = legacy[':root, :root[data-theme="dark"]'], legacy[':root[data-theme="light"]']
    colliding = sorted((set(basecoat[":root"]) | set(basecoat[".dark"])) & (set(dark) | set(light)))
    assert colliding == ["--accent", "--muted", "color-scheme"]  # si la liste bouge, v3.css doit suivre
    v3 = {prelude: _decls(body) for prelude, body in _css_top_level(re.sub(r"/\*.*?\*/", "", _static("v3.css"), flags=re.S))
          if prelude.startswith(":root") and body}
    restored_dark, restored_light = v3[':root, :root[data-theme="dark"]'], v3[':root[data-theme="light"]']
    for name in colliding:
        assert restored_dark[name] == dark[name], name
        assert restored_light[name] == light[name], name
    # et rien d'autre sur :root dans v3.css : les jetons v3 vivent sur .v3
    assert set(restored_dark) == set(colliding) and set(restored_light) == set(colliding)
    assert "/* >>> legacy-tokens */" in _static("v3.css") and "/* <<< legacy-tokens */" in _static("v3.css")


def test_style_css_first_rule_is_the_legacy_token_block():
    # Un commentaire d'en-tête mal fermé avait avalé ce bloc : la page perdait --font, --r, --topbar...
    rules = [p for p, body in _css_top_level(re.sub(r"/\*.*?\*/", "", _static("style.css"), flags=re.S)) if body is not None]
    assert rules[0] == ":root"
    assert rules[1] == ':root, :root[data-theme="dark"]' and rules[2] == ':root[data-theme="light"]'


def test_sidebar_keeps_the_v2_width_while_legacy_screens_remain():
    css = _static("v3.css")
    assert "var(--nav-w, 236px)" in css  # largeur v2 : le contenu des écrans non migrés ne bouge pas


# --- heure de Paris --------------------------------------------------------------------------------------------

def test_shell_scripts_never_format_an_hour_without_time_zone():
    for name in ("app.js", "ui.js"):
        js = "\n".join(l for l in _static(name).splitlines() if not l.lstrip().startswith(("//", "/*", "*")))
        for match in re.finditer(r"toLocale(?:Time|Date)?String\(([^;]*)", js):
            line = match.group(0).split("\n")[0]
            assert "minimumFractionDigits" in line or "timeZone" in line, f"{name} : {line[:100]}"
    assert 'const CLIPPER_TZ = "Europe/Paris"' in _static("ui.js")


@NODE
def test_node_check_of_the_shell_scripts():
    for name in ("app.js", "ui.js", "screens.js"):
        subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True, text=True, encoding="utf-8", check=True)
