"""Socle Basecoat + Idiomorph vendored (ADR-a308, SPEC-7715, TASK-5b54d0a590bb).

Aucun test ne touche le réseau : le téléchargement n'est jamais appelé ici,
on vérifie les fichiers commités (sha256 de VERSIONS), le préfixage `.v3` (mode de migration `--prefix`)
et le mode d'arrivée `--no-prefix` (sélecteurs globaux, clôture U14).
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "vendor_basecoat.py"
VENDOR = ROOT / "clipper" / "web" / "static" / "vendor"


def _load_script():
    spec = importlib.util.spec_from_file_location("vendor_basecoat", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vb = _load_script()


# --- préfixage CSS -------------------------------------------------------

def test_prefix_plain_selector():
    assert vb.prefix_css(".btn{color:red}") == ".v3 .btn{color:red}"


def test_prefix_each_selector_of_a_list_at_top_level_commas_only():
    assert vb.prefix_css(".a, .b:is(.c,.d){x:1}") == ".v3 .a,.v3 .b:is(.c,.d){x:1}"


def test_prefix_descends_into_conditional_and_layer_at_rules():
    css = "@media (width>=40rem){.a{x:1}}@supports (display:grid){.b{y:2}}@layer base{.c{z:3}}"
    expected = "@media (width>=40rem){.v3 .a{x:1}}@supports (display:grid){.v3 .b{y:2}}@layer base{.v3 .c{z:3}}"
    assert vb.prefix_css(css) == expected


def test_statement_at_rule_is_kept_and_rules_after_it_prefixed():
    assert vb.prefix_css("@layer properties;.a{x:1}") == "@layer properties;.v3 .a{x:1}"


def test_keyframes_property_font_face_are_kept_verbatim():
    css = (
        "@keyframes spin{from{transform:rotate(0)}to{transform:rotate(1turn)}}"
        "@property --x{syntax:'*';inherits:false}"
        "@font-face{font-family:A;src:url(a.woff2)}"
    )
    assert vb.prefix_css(css) == css


def test_whitelisted_selectors_are_kept_unprefixed():
    css = ":root{--a:1}.dark{--a:2}html{x:1}"
    assert vb.prefix_css(css) == css


def test_braces_and_commas_inside_strings_do_not_split_rules():
    assert vb.prefix_css('.a::after{content:"{,}"}') == '.v3 .a::after{content:"{,}"}'


def test_top_level_license_comment_is_kept_in_place():
    assert vb.prefix_css("/*! lic */.a{x:1}") == "/*! lic */.v3 .a{x:1}"


def test_unknown_at_rule_is_refused():
    with pytest.raises(ValueError, match="@foo"):
        vb.prefix_css("@foo x{.a{x:1}}")


def test_nested_rule_inside_a_style_block_is_refused():
    with pytest.raises(ValueError):
        vb.prefix_css(".a{&:hover{x:1}}")


# --- fichiers commités ---------------------------------------------------

def test_vendored_selectors_are_global_after_the_closure_of_the_migration():
    css = (VENDOR / "basecoat.cdn.min.css").read_text(encoding="utf-8")
    selectors = vb.style_selectors(css)
    assert selectors, "aucun sélecteur lu : le parseur ne voit rien"
    assert [s for s in selectors if s.startswith(".v3")] == []
    assert ".btn" in selectors and any(s.startswith(".dialog") for s in selectors)


def _fake_fetch(pin):
    if pin["name"] == "basecoat-css":
        return {"basecoat.cdn.min.css": b".btn{color:red}", "basecoat.all.min.js": b"//js", "LICENSE-basecoat.md": b"mit"}
    return {"idiomorph.min.js": b"//m", "LICENSE-idiomorph.txt": b"0bsd"}


def test_build_writes_basecoat_css_unprefixed_by_default_and_prefixed_on_demand(tmp_path, monkeypatch):
    monkeypatch.setattr(vb, "_fetch", _fake_fetch)
    vb.build(tmp_path / "plain")
    assert (tmp_path / "plain" / "basecoat.cdn.min.css").read_text(encoding="utf-8") == ".btn{color:red}"
    vb.build(tmp_path / "pre", prefix=True)
    assert (tmp_path / "pre" / "basecoat.cdn.min.css").read_text(encoding="utf-8") == ".v3 .btn{color:red}"
    assert vb.check(tmp_path / "plain") == []


def test_main_no_prefix_flag_builds_without_prefix_and_prefix_flag_with_it(monkeypatch):
    calls = []
    monkeypatch.setattr(vb, "build", lambda directory, prefix=False: calls.append(prefix))
    assert vb.main(["--no-prefix"]) == 0 and vb.main([]) == 0 and vb.main(["--prefix"]) == 0
    assert calls == [False, False, True]
    with pytest.raises(SystemExit):
        vb.main(["--prefix", "--no-prefix"])


def test_vendored_css_has_no_external_fetch():
    css = (VENDOR / "basecoat.cdn.min.css").read_text(encoding="utf-8")
    assert not re.search(r"@import|url\(\s*['\"]?(https?:)?//", css)


def test_vendored_js_passes_node_check():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent du PATH")
    for name in ("basecoat.all.min.js", "idiomorph.min.js"):
        result = subprocess.run([node, "--check", str(VENDOR / name)], capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_versions_cites_pinned_upstream_versions():
    text = (VENDOR / "VERSIONS").read_text(encoding="utf-8")
    assert "basecoat-css 1.0.2" in text
    assert "idiomorph 0.8.0" in text


def test_licences_are_committed_next_to_the_code():
    assert (VENDOR / "LICENSE-basecoat.md").read_text(encoding="utf-8").strip()
    assert (VENDOR / "LICENSE-idiomorph.txt").read_text(encoding="utf-8").strip()


def test_check_passes_on_committed_files():
    assert vb.check(VENDOR) == []


def test_check_detects_a_tampered_file(tmp_path):
    copy = tmp_path / "vendor"
    shutil.copytree(VENDOR, copy)
    with open(copy / "idiomorph.min.js", "a", encoding="utf-8") as handle:
        handle.write("\n// modifié\n")
    problems = vb.check(copy)
    assert problems and any("idiomorph.min.js" in p for p in problems)


def test_check_detects_a_missing_file(tmp_path):
    copy = tmp_path / "vendor"
    shutil.copytree(VENDOR, copy)
    (copy / "basecoat.all.min.js").unlink()
    problems = vb.check(copy)
    assert problems and any("basecoat.all.min.js" in p for p in problems)


def test_check_command_returns_zero_on_committed_files():
    assert vb.main(["--check"]) == 0
