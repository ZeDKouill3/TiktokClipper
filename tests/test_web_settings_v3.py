"""Écran Réglages v3 (TASK-2f3bcef584ca, U12 ; ADR-a308, SPEC-7715).

Aucun test ne touche le réseau ni un navigateur : les fichiers statiques sont lus tels quels.
"""

from __future__ import annotations

import re
from pathlib import Path

from clipper.web import app as web_app

STATIC = Path(__file__).resolve().parents[1] / "clipper" / "web" / "static"

# Champs que l'écran rend sans que le serveur les serve en « defaults » : la clé d'API de la veille
# (écriture seule, libellée comme les autres) et les tables de backend (rendues par niveau).
_BACKEND_TABLES = ("claude_cli", "claude_api", "ollama")


def _static(*parts: str) -> str:
    return STATIC.joinpath(*parts).read_text(encoding="utf-8")


def _labels() -> dict[str, str]:
    js = _static("screens", "settings.js")
    start = js.index("const SET_LABELS = {")
    block = js[start:js.index("\n};", start)]
    return {m.group(1): m.group(2) for m in re.finditer(r'^\s*"([a-z_0-9.]+)": "([^"\n]+)",?\s*$', block, re.M)}


def _displayed_keys() -> set[str]:
    """Chemin (ex. ``llm.claude_cli.timeout``) de chaque champ que l'écran rend (setField et ses variantes)."""
    keys = {"mode", "workspace_dir", "output_dir"}
    for section in web_app._SETTINGS_SECTIONS:
        for key, doc in web_app._defaults_documentation(section).items():
            value = doc["default"]
            if section == "web" and key == "token":
                continue                                    # jamais affiché ici (section Accès)
            if section == "llm":
                if key == "usages":
                    continue                                # table « Modèle par usage », pas un champ
                if key in _BACKEND_TABLES:
                    keys |= {f"llm.{key}.{k}" for k, v in value.items() if k != "models" and not isinstance(v, dict)}
                    keys |= {f"llm.{key}.models.{tier}" for tier in value.get("models", {})}
                    continue
            if section == "veille" and key in web_app._VEILLE_SECRETS:
                continue                                    # rendues à part, en écriture seule
            if not isinstance(value, dict):
                keys.add(f"llm.{key}" if section == "llm" else f"{section}.{key}")
    keys |= {f"veille.{k}" for k in web_app._VEILLE_SECRETS}
    return keys


def test_every_displayed_settings_key_has_a_french_label():
    labels = _labels()
    expected = _displayed_keys()
    assert expected - set(labels) == set(), "clés sans libellé"
    assert set(labels) - expected == set(), "libellés de clés qui n'existent plus"


def test_a_settings_label_is_a_sentence_not_the_technical_key():
    for path, label in _labels().items():
        assert label != path.rsplit(".", 1)[-1] and "_" not in label, f"{path} : libellé technique ({label!r})"
        assert label[0].isupper() or label[0].isdigit(), f"{path} : libellé sans majuscule ({label!r})"
        assert "—" not in label, path                       # pas de tiret cadratin (DIRECTION.md, principe 10)


def test_the_field_shows_the_french_label_with_its_key_as_a_small_mono_reference():
    js = _static("screens", "settings.js")
    assert "SET_LABELS[path.join(\".\")]" in js
    assert 'class="mono set-key"' in js
    assert "set-key" in _static("console.css")


def test_save_button_sits_in_a_sticky_foot():
    js = _static("screens", "settings.js")
    foot = re.search(r'class="sticky-foot[^"]*"[\s\S]*?</div>', js)
    assert foot is not None, "pas de .sticky-foot"
    assert "data-set-save" in foot.group(0) and "data-set-cancel" in foot.group(0)
    css = _static("console.css")
    rule = css[css.index(".sticky-foot {"):]
    rule = rule[:rule.index("}")]
    assert "position: sticky" in rule and "bottom:" in rule


def test_comments_lost_warning_is_one_line_under_the_save_button():
    js = _static("screens", "settings.js")
    foot = re.search(r'class="sticky-foot[^"]*"[\s\S]*?</div>', js).group(0)
    # le bandeau est rendu par setWarning() : un appel dans le pied, après le bouton Enregistrer
    assert "${setWarning()}" in foot and "data-set-warning" in js
    assert foot.index("data-set-save") < foot.index("${setWarning()}")   # sous le bouton
    assert "reason set-warning" not in js                   # plus de bandeau en tête de formulaire
    assert "commentaires du fichier perdus" in js           # le texte d'avertissement reste


def test_settings_has_no_migration_class_and_its_css_lives_in_console_css():
    js = _static("screens", "settings.js")
    assert 'classList.add("v3")' not in js
    console = _static("console.css")
    assert not (STATIC / "style.css").exists()
    assert "/* >>> settings */" in console and "/* <<< settings */" in console
    for selector in (".set-layout", ".set-nav", ".set-backend", ".set-usages", ".sticky-foot", ".set-key"):
        assert selector in console, selector


def test_settings_block_is_the_last_block_of_v3_css_and_stays_in_budget():
    v3 = _static("console.css")
    assert len(v3.encode("utf-8")) <= 163_840
    # bloc ferme, pas forcement le dernier : les ecrans migres ensuite ajoutent le leur a la suite
    assert v3.index("/* >>> settings */") < v3.index("/* <<< settings */")
    block = v3[v3.rindex("/* >>> settings */"):]
    for legacy_token in ("var(--surface", "var(--line", "var(--text", "var(--r)", "var(--r-lg", "var(--topbar)"):
        assert legacy_token not in block, legacy_token      # jetons v3 seulement


def test_settings_sticky_summary_stays_on_screen():
    v3 = _static("console.css")
    rule = v3[v3.index(".set-nav {"):]
    rule = rule[:rule.index("}")]
    assert "position: sticky" in rule and "top:" in rule
