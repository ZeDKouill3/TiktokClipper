"""Écran Styles v3 (TASK-fecba79ca3d4, U9 ; ADR-a308, SPEC-7715).

Aucun test ne touche le réseau ni un navigateur : les fichiers statiques sont lus tels quels.
"""

from __future__ import annotations

import re
from pathlib import Path

from clipper.web import app as web_app

STATIC = Path(__file__).resolve().parents[1] / "clipper" / "web" / "static"


def _static(*parts: str) -> str:
    return (STATIC / Path(*parts)).read_text(encoding="utf-8")


def _labels() -> dict[str, str]:
    js = _static("screens", "channels.js")
    start = js.index("const FIELD_LABELS = {")
    block = js[start:js.index("\n};", start)]
    return {m.group(1): m.group(2) for m in re.finditer(r'^\s*"([a-z_]+\.[a-z_0-9]+)": "([^"\n]+)",?\s*$', block, re.M)}


def test_every_displayed_config_key_has_a_french_label():
    labels = _labels()
    expected = {f"{section}.{key}" for section in web_app._CHANNEL_FORM_SECTIONS
                for key in web_app._defaults_documentation(section)}
    assert expected - set(labels) == set(), "clés sans libellé"
    assert set(labels) - expected == set(), "libellés de clés qui n'existent plus"


def test_a_label_is_a_sentence_not_the_technical_key():
    for ident, label in _labels().items():
        key = ident.split(".", 1)[1]
        assert label != key and "_" not in label, f"{ident} : libellé technique ({label!r})"
        assert label[0].isupper() or label[0].isdigit(), f"{ident} : libellé sans majuscule ({label!r})"
        assert "—" not in label, ident                      # pas de tiret cadratin (DIRECTION.md, principe 10)


def test_field_shows_the_label_with_the_key_as_a_small_mono_reference():
    js = _static("screens", "channels.js")
    assert "FIELD_LABELS[`${section}.${key}`]" in js
    assert 'class="mono chan-key"' in js


def test_delete_lives_in_the_ellipsis_menu_not_in_the_toolbar():
    js = _static("screens", "channels.js")
    edit = js[js.index("function chEditHtml"):js.index("/* Preset à envoyer")]
    menu = edit[edit.index('class="vmenu-wrap"'):]
    assert "data-chan-menu" in edit and 'aria-haspopup="menu"' in menu
    assert 'role="menu"' in menu and "data-chan-delete" in menu and "Supprimer le style" in menu
    toolbar = edit[:edit.index('class="vmenu-wrap"')]
    assert "data-chan-delete" not in toolbar and "btn-bad" not in edit
    assert "confirmDialog" in js and "confirm=true" in js    # la confirmation reste


def test_channels_is_a_v3_screen_and_its_css_left_style_css():
    js = _static("screens", "channels.js")
    assert 'section.classList.add("v3")' in js
    # l'éditeur d'agencement enveloppe Screens.channels.render : sans la classe, ses règles `.v3 .ly-*` ne s'appliquent pas
    assert 'section.classList.add("v3")' in _static("screens", "layout.js")
    legacy, v3 = _static("style.css"), _static("v3.css")
    assert "/* >>> channels */" not in legacy and "/* >>> channels */" in v3 and "/* <<< channels */" in v3
    for selector in (".chan-sec", ".chan-fields", ".chan-field.inherited", ".chan-preview", ".ly-grid", ".ly-zone.off",
                     ".lb-z-video", ".ch-card", ".chan-model", ".vmenu[hidden]"):
        assert f".v3 {selector}" in v3, selector
        assert selector + " {" not in legacy and selector + "," not in legacy, selector


def test_channels_block_is_a_closed_block_of_v3_css_and_stays_in_budget():
    # les ecrans migres apres U9 ajoutent leur bloc a la suite : on exige un bloc ferme, pas la derniere place
    v3 = _static("v3.css")
    assert len(v3.encode("utf-8")) <= 124_162
    assert v3.rindex("/* >>> channels */") > v3.rindex("/* <<< dashboard */")
    start = v3.rindex("/* >>> channels */")
    end = v3.index("/* <<< channels */", start)
    block = v3[start:end]
    assert "var(--surface" not in block and "var(--line" not in block and "var(--faint" not in block  # jetons v3 seulement
