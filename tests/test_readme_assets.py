"""TASK-44aae8fe7bc1 : README pro (logo, animations SVG, sections). Le
contenu redactionnel se lit a l'oeil ; ce fichier verifie mecaniquement les
clauses du critere qui peuvent l'etre : sections presentes, liens relatifs
valides, logo reference, badges, SVG valides sans <script>, animation CSS
pure (pas de JavaScript), duree de boucle 15-25 s, aucun identifiant reel ou
chemin perso, ordre des etapes du pipeline, lisibilite clair/sombre.

TASK-dbb2 : README refait avec captures et GIF de la console (docs/assets/readme/,
generes par tools/readme_shots/) : chaque image et GIF reference existe, poids
maximal respecte, aucun nom reel interdit, liens et ancres valides, et le
script de capture n'utilise que le dossier temporaire (jamais le vrai
workspace/state)."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DEMO_TERMINAL_SVG = ROOT / "docs" / "assets" / "demo-terminal.svg"
PIPELINE_SVG = ROOT / "docs" / "assets" / "pipeline.svg"

REQUIRED_SECTIONS = [
    "Ce que ça fait",
    "Démo",
    "Points forts",
    "Installation",
    "Démarrage rapide",
    "Formats",
    "Styles, grille gaming et CTA abonnement",
    "Modes review/auto",
    "La console en images",
    "Interface web",
    "Publier sur TikTok",
    "Statistiques TikTok",
    "Coûts et performances",
    "Configuration",
    "Documentation",
    "Développement",
    "Limites et feuille de route",
]

# Identifiants/chemins reels vus dans les journaux sources (research/, hors
# scope git) : ne doivent jamais fuiter dans un artefact versionne.
LEAKED_TOKENS = (
    "7VaA8XUKrAY",
    "WVjOSRFWm4c",
    "ivl0nxa3C7o",
    "2887271276",
    "C:\\Users",
)

# Mots sensibles (pseudo d'une chaine amie, nom d'utilisateur Windows, dossier perso) : seules leurs empreintes
# sha256 (du mot en minuscules) sont ecrites ici, couples (longueur, empreinte) ; les mots ne figurent en clair
# nulle part dans les fichiers suivis. Le texte controle est cherche par fenetres glissantes de ces longueurs.
SENSITIVE_SHA256 = (
    (7, "4da6a20ea297d6aff097b64dae3fbbe64823bf35a8b760abcd535041ba3bfe09"),
    (5, "bbb6fa51708957e6b8f72b02fbc5cfca676c5f01b45fe9d05c18aa5efe753d05"),
    (12, "01fcdb58b2507fe5cb747317f4066135797057de7971612e6db0992e41d7bb82"),
)


def sensitive_hits(text: str, hashes=None) -> list[str]:
    """Une entree « mot sensible N a la position P » par fenetre du texte dont l'empreinte est dans la liste."""
    pairs = SENSITIVE_SHA256 if hashes is None else hashes
    lowered = text.lower()
    hits = []
    for number, (length, digest) in enumerate(pairs, start=1):
        for start in range(len(lowered) - length + 1):
            if hashlib.sha256(lowered[start:start + length].encode("utf-8")).hexdigest() == digest:
                hits.append(f"mot sensible {number} a la position {start}")
    return hits


def test_sensitive_guard_detects_a_word_by_its_hash_and_ignores_clean_text():
    probe = ((5, hashlib.sha256(b"zorgl").hexdigest()),)
    assert sensitive_hits("un texte avec le mot ZoRgL au milieu", probe) == ["mot sensible 1 a la position 21"]
    assert sensitive_hits("un texte propre", probe) == []


PIPELINE_STEPS = (
    "download", "transcribe", "scenes", "audio", "moments", "vision",
    "parts", "captions", "reframe", "subtitles", "render", "qa",
)


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def _relative_link_targets(text: str) -> list[str]:
    targets = re.findall(r"\]\(([^)]+)\)", text)  # [texte](cible) et ![alt](cible)
    targets += re.findall(r'src="([^"]+)"', text)  # <img src="cible">
    targets += re.findall(r'srcset="([^"]+)"', text)  # <source srcset="cible">
    cleaned = []
    for target in targets:
        target = target.split(" ", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        cleaned.append(target)
    return cleaned


def test_readme_has_all_required_sections():
    text = _readme_text()
    headings = re.findall(r"^#{1,3}\s+(.+)$", text, flags=re.MULTILINE)
    for section in REQUIRED_SECTIONS:
        assert any(section in heading for heading in headings), f"section manquante : {section}"


def test_readme_relative_links_point_to_existing_files():
    text = _readme_text()
    targets = _relative_link_targets(text)
    assert targets, "aucun lien relatif trouve dans README.md"
    for target in targets:
        resolved = (README.parent / target).resolve()
        assert resolved.exists(), f"lien mort dans README.md : {target}"


def test_readme_shows_the_project_logo():
    text = _readme_text()
    assert "logo.svg" in text
    assert any("logo.svg" in target for target in _relative_link_targets(text))


def _current_version() -> str:
    import tomllib

    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def test_readme_has_version_and_python_badges():
    text = _readme_text()
    assert f"version-{_current_version()}" in text
    assert "3.11" in text


def test_readme_has_no_licence_badge_when_no_license_file_exists():
    assert not (ROOT / "LICENSE").exists()
    assert not (ROOT / "LICENSE.txt").exists()
    assert not (ROOT / "LICENSE.md").exists()
    text = _readme_text().lower()
    assert not re.search(r"badge/licen[sc]e", text)


@pytest.mark.parametrize("svg_path", [DEMO_TERMINAL_SVG, PIPELINE_SVG], ids=["demo-terminal", "pipeline"])
def test_svg_is_valid_xml(svg_path):
    ET.parse(svg_path)


@pytest.mark.parametrize("svg_path", [DEMO_TERMINAL_SVG, PIPELINE_SVG], ids=["demo-terminal", "pipeline"])
def test_svg_has_no_script_tag(svg_path):
    text = svg_path.read_text(encoding="utf-8").lower()
    assert "<script" not in text


@pytest.mark.parametrize("svg_path", [DEMO_TERMINAL_SVG, PIPELINE_SVG], ids=["demo-terminal", "pipeline"])
def test_svg_has_no_leaked_real_identifier_or_personal_path(svg_path):
    text = svg_path.read_text(encoding="utf-8")
    for leaked in LEAKED_TOKENS:
        assert leaked not in text, f"{leaked!r} fuite dans {svg_path.name}"
    assert not sensitive_hits(text), f"{svg_path.name} : {sensitive_hits(text)}"


def test_demo_terminal_svg_uses_css_keyframes():
    text = DEMO_TERMINAL_SVG.read_text(encoding="utf-8")
    assert "@keyframes" in text


def test_demo_terminal_svg_loop_duration_between_15_and_25_seconds():
    text = DEMO_TERMINAL_SVG.read_text(encoding="utf-8")
    durations = [float(d) for d in re.findall(r"animation(?:-duration)?:\s*(?:[A-Za-z][\w-]*\s+)?([\d.]+)s", text)]
    assert durations, "aucune duree d'animation CSS trouvee dans demo-terminal.svg"
    assert all(15.0 <= d <= 25.0 for d in durations), durations


def test_demo_terminal_svg_replays_the_real_pipeline_command():
    text = DEMO_TERMINAL_SVG.read_text(encoding="utf-8")
    assert "python -m clipper -v run" in text


def test_demo_terminal_svg_command_line_visible_without_animation():
    """Une capture statique (t=0, animation non jouee) ne doit pas etre vide :
    au minimum la ligne de commande a une opacite de base de 1, independante
    du @keyframes qui la revele."""
    text = DEMO_TERMINAL_SVG.read_text(encoding="utf-8")
    match = re.search(r'<text[^>]*class="[^"]*prompt[^"]*"[^>]*>', text)
    assert match, "aucune ligne de commande (classe prompt) trouvee"
    prompt_tag = match.group(0)
    assert re.search(r"opacity:\s*1\b", prompt_tag), (
        "la ligne de commande n'a pas d'opacite de base 1 : invisible si l'animation ne joue pas"
    )


def test_demo_terminal_svg_shows_the_step_sequence():
    text = DEMO_TERMINAL_SVG.read_text(encoding="utf-8")
    positions = [text.index(f"etape {step}") for step in PIPELINE_STEPS]
    assert positions == sorted(positions)


def test_pipeline_svg_shows_all_steps_in_order():
    text = PIPELINE_SVG.read_text(encoding="utf-8")
    positions = [text.index(f">{step}<") for step in PIPELINE_STEPS]
    assert positions == sorted(positions)


def test_pipeline_svg_shows_the_output_contract():
    text = PIPELINE_SVG.read_text(encoding="utf-8")
    assert "output/" in text and ".mp4" in text and ".json" in text


def test_pipeline_svg_readable_in_light_and_dark_github_theme():
    text = PIPELINE_SVG.read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in text


# --------------------------------------------------------------------------
# TASK-dbb2 : captures et GIF generes par tools/readme_shots/
# --------------------------------------------------------------------------

SHOTS_DIR = ROOT / "tools" / "readme_shots"
ASSETS_DIR = ROOT / "docs" / "assets" / "readme"
SHOT_NAMES = ("tableau-de-bord", "veille", "veille-selection", "videos", "video-fiche", "radar-jury", "clips",
              "clip-fiche", "publication", "stats-ensemble", "stats-video", "stats-retention")
# TASK-5142ea3f07cc : captures refaites en theme clair uniquement (plus de variante sombre, plus de <picture>) ;
# l'ecran Comptes n'est plus capture (adresses e-mail).
THEMES = ("light",)
GIF_NAMES = ("progression-en-direct", "radar-du-jury", "nouvelle-publication")
MAX_IMAGE_BYTES = 400 * 1024
MAX_GIF_BYTES = 2 * 1024 * 1024
IMAGE_SUFFIXES = (".png", ".webp", ".gif", ".svg")

# Liste de controle : noms de personnes, de chaines ou de jeux reels qui ne doivent apparaitre ni dans le README,
# ni dans les donnees de demonstration (depot public, donnees 100 % neutres).
FORBIDDEN_NAMES = (
    "gta", "rockstar", "vice city", "zerator", "squeezie", "inoxtag", "michou", "ponce", "locklear",
    "kameto", "gotaga", "domingo", "amixem", "mister v", "ninja", "pewdiepie", "mrbeast", "tiktok.com/@",
)


def _load(name: str):
    sys.path.insert(0, str(SHOTS_DIR))
    try:
        spec = importlib.util.spec_from_file_location(f"readme_shots_{name}", SHOTS_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SHOTS_DIR))


def _expected_assets() -> list[Path]:
    return ([ASSETS_DIR / f"{name}-{theme}.webp" for name in SHOT_NAMES for theme in THEMES]
            + [ASSETS_DIR / f"{name}.gif" for name in GIF_NAMES])


def _slug(heading: str) -> str:
    """Ancre GitHub d'un titre : minuscules, ponctuation retiree (les accents restent), espaces en tirets."""
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s", "-", text)


def test_every_expected_screenshot_and_gif_exists():
    for path in _expected_assets():
        assert path.is_file(), f"image attendue absente : {path.relative_to(ROOT)} (lance tools/readme_shots/capture.py)"


def test_readme_uses_light_screenshots_only_without_picture_or_dark_variant():
    text = _readme_text()
    assert "<picture" not in text and "<source" not in text
    assert "-dark" not in text
    assert not list(ASSETS_DIR.glob("*-dark.*")), "variante sombre restante dans docs/assets/readme/"
    assert not (ASSETS_DIR / "comptes-light.webp").exists(), "l'ecran Comptes montre des e-mails : jamais capture"
    cited = set(re.findall(r"docs/assets/readme/([\w.-]+\.webp)", text))
    assert cited == {f"{name}-light.webp" for name in SHOT_NAMES}
    on_disk = {path.name for path in ASSETS_DIR.glob("*.webp")}
    assert on_disk == cited, f"images non citees dans docs/assets/readme/ : {sorted(on_disk - cited)}"


def test_readme_references_every_generated_image_and_gif():
    text = _readme_text()
    for path in _expected_assets():
        assert f"docs/assets/readme/{path.name}" in text, f"{path.name} n'est pas utilise dans README.md"


def test_every_image_referenced_by_the_readme_exists_and_respects_its_weight_limit():
    targets = [t for t in _relative_link_targets(_readme_text()) if t.lower().endswith(IMAGE_SUFFIXES)]
    assert targets
    for target in targets:
        path = (README.parent / target).resolve()
        assert path.is_file(), f"image absente : {target}"
        if "docs/assets/readme/" not in target.replace("\\", "/"):
            continue
        limit = MAX_GIF_BYTES if path.suffix == ".gif" else MAX_IMAGE_BYTES
        assert path.stat().st_size <= limit, f"{target} pese {path.stat().st_size} octets (maximum {limit})"


def test_generated_assets_are_real_images_of_the_expected_size():
    from PIL import Image

    for path in _expected_assets():
        with Image.open(path) as image:
            if path.suffix == ".webp":
                assert image.size == (1440, 900), f"{path.name} : {image.size}"
            else:
                assert image.n_frames >= 4, f"{path.name} : un GIF d'au moins 4 images est attendu"


def test_every_generated_image_has_a_non_empty_alt_text_in_the_readme():
    text = _readme_text()
    for tag in re.findall(r"<img\b[^>]*>", text):
        alt = re.search(r'alt="([^"]*)"', tag)
        assert alt and alt.group(1).strip(), f"image sans texte alternatif : {tag}"


def test_readme_has_no_forbidden_real_name():
    text = _readme_text().lower()
    for name in FORBIDDEN_NAMES:
        assert name not in text, f"nom interdit dans README.md : {name!r}"
    for token in LEAKED_TOKENS:
        assert token.lower() not in text, f"identifiant reel dans README.md : {token!r}"
    assert not sensitive_hits(text), f"README.md : {sensitive_hits(text)}"


def test_readme_internal_anchors_point_to_existing_headings():
    text = _readme_text()
    slugs = {_slug(h) for h in re.findall(r"^#{1,6}\s+(.+)$", text, flags=re.MULTILINE)}
    anchors = re.findall(r"\]\(#([^)]+)\)", text)
    assert anchors
    for anchor in anchors:
        assert anchor in slugs, f"ancre morte dans README.md : #{anchor}"


def test_readme_version_badge_matches_pyproject():
    assert f"version-{_current_version()}" in _readme_text()


def test_readme_links_the_documentation_set():
    text = _readme_text()
    targets = ["docs/GUIDE.md", "docs/versions.md", "docs/tiktok-cadence.md", "CHANGELOG.md"]
    notes = ROOT / "docs" / "releases" / f"v{_current_version()}.md"
    if notes.exists():
        targets.append(f"docs/releases/v{_current_version()}.md")
    for target in targets:
        assert f"]({target})" in text, f"lien vers {target} absent de README.md"


def test_readme_documents_install_quickstart_tiktok_risks_and_stats():
    text = _readme_text()
    for needle in ("tools/setup.ps1", "uv venv", "python -m clipper serve", "builtin:gaming", "captcha", "Statistiques TikTok",
                   "tools/readme_shots/capture.py"):
        assert needle in text, needle


def test_readme_documents_the_clipper_bat_launcher_and_its_shortcut():
    text = _readme_text()
    assert (ROOT / "Clipper.bat").is_file() and (ROOT / "tools" / "creer-raccourci.ps1").is_file()
    assert (ROOT / "tools" / "clipper.ico").is_file()
    for needle in ("Clipper.bat", "tools/creer-raccourci.ps1", "Clipper.lnk", "http://127.0.0.1:8000"):
        assert needle in text, needle
    start = text.index("## Démarrage rapide")
    assert "Clipper.bat" in text[start:text.index("## Formats")], "le lanceur doit etre dans la section de lancement"


def test_readme_links_the_social_preview_image_that_exists():
    assert (ROOT / "docs" / "assets" / "social-preview.png").is_file()


# ---- le script de capture n'utilise que le dossier temporaire


def test_capture_guard_accepts_only_a_folder_under_the_system_temp_dir(tmp_path):
    capture = _load("capture")

    assert capture.guard_demo_root(tmp_path) == tmp_path.resolve()
    with pytest.raises(capture.CaptureError, match="temporaire"):
        capture.guard_demo_root(ROOT / "workspace")
    with pytest.raises(capture.CaptureError):
        capture.guard_demo_root(Path.home() / "workspace")


def test_capture_guard_refuses_a_folder_inside_the_repository(monkeypatch):
    capture = _load("capture")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(ROOT))   # meme si le « temp » etait le depot

    with pytest.raises(capture.CaptureError, match="dépôt"):
        capture.guard_demo_root(ROOT / "workspace" / "x")


def test_demo_write_helper_refuses_to_leave_the_demo_folder(tmp_path):
    demo = _load("demo_data")

    assert demo._inside(tmp_path, tmp_path / "workspace" / "a.json")
    with pytest.raises(demo.DemoError, match="hors du dossier"):
        demo._inside(tmp_path, tmp_path.parent / "ailleurs.json")
    with pytest.raises(demo.DemoError, match="hors du dossier"):
        demo._inside(tmp_path, ROOT / "state" / "accounts.json")


def test_capture_sources_never_name_the_real_workspace_state_or_output_folders():
    for name in ("capture.py", "demo_data.py", "serve_demo.py"):
        text = (SHOTS_DIR / name).read_text(encoding="utf-8")
        assert not re.search(r"Path\((?:\"|')(?:workspace|state|output|presets)(?:\"|')\)", text), name
        assert "REPO_ROOT / \"workspace\"" not in text and "REPO_ROOT / \"state\"" not in text, name
    assert "tempfile.mkdtemp" in (SHOTS_DIR / "capture.py").read_text(encoding="utf-8")


def test_capture_run_builds_in_a_temp_folder_and_removes_it_even_on_failure(monkeypatch, tmp_path):
    capture = _load("capture")
    made: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        made.append(Path(real_mkdtemp(*args, **kwargs)))
        return str(made[-1])

    def failing_build(root, **kwargs):
        assert root == made[0] and (root / "x").parent == root
        raise capture.demo_data.DemoError("echec simule")

    monkeypatch.setattr(capture.tempfile, "mkdtemp", recording_mkdtemp)
    monkeypatch.setattr(capture.demo_data, "build_demo", failing_build)
    before = sorted(p.name for p in ASSETS_DIR.iterdir())

    with pytest.raises(capture.demo_data.DemoError, match="simule"):
        capture.run()

    assert len(made) == 1 and not made[0].exists()          # dossier temporaire supprime
    assert capture.guard_demo_root(made[0])                 # et il etait bien sous le dossier temporaire
    assert sorted(p.name for p in ASSETS_DIR.iterdir()) == before   # rien d'ecrit dans le depot


def test_capture_rejects_an_unknown_setting():
    capture = _load("capture")
    with pytest.raises(capture.CaptureError, match="inconnus"):
        capture._settings({"viewpoort": (1, 1)})


def test_optimize_image_keeps_the_limit_and_fails_explicitly_when_it_cannot(tmp_path):
    import io
    import random

    from PIL import Image

    capture = _load("capture")
    settings = capture._settings({})
    flat = io.BytesIO()
    Image.new("RGB", (1440, 900), (30, 30, 30)).save(flat, format="PNG")
    size = capture.optimize_image(flat.getvalue(), tmp_path / "plat.webp", settings)
    assert 0 < size <= settings["image_max_kb"] * 1024 and (tmp_path / "plat.webp").stat().st_size == size

    rng = random.Random(1)
    noise = Image.frombytes("RGB", (400, 400), bytes(rng.randrange(256) for _ in range(400 * 400 * 3)))
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    with pytest.raises(capture.CaptureError, match="dépasse"):
        capture.optimize_image(buffer.getvalue(), tmp_path / "bruit.webp", capture._settings({"image_max_kb": 1}))
    assert not (tmp_path / "bruit.webp").exists()


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent du PATH")


@needs_ffmpeg
def test_frames_to_gif_builds_a_gif_and_fails_explicitly_above_the_weight_limit(tmp_path):
    from PIL import Image

    capture = _load("capture")
    frames = tmp_path / "frames"
    frames.mkdir()
    for index in range(4):
        Image.new("RGB", (64, 40), (index * 60, 20, 90)).save(frames / f"f{index + 1:03d}.png")
    settings = capture._settings({"gif_width": 64})

    size = capture.frames_to_gif(frames, tmp_path / "ok.gif", settings)
    assert size > 0 and Image.open(tmp_path / "ok.gif").n_frames >= 2

    with pytest.raises(capture.CaptureError, match="dépasse"):
        capture.frames_to_gif(frames, tmp_path / "gros.gif", capture._settings({"gif_width": 64, "gif_max_mb": 0.00001}))


@needs_ffmpeg
def test_demo_workspace_is_neutral_complete_and_written_only_under_its_folder(tmp_path):
    import json

    demo = _load("demo_data")
    root = tmp_path / "demo"
    root.mkdir()
    cwd = Path.cwd()
    repo_before = {name: (ROOT / name).exists() for name in ("workspace", "output", "state", "presets")}

    info = demo.build_demo(root, settings={"source_seconds": 30, "stats_days": 5})

    assert Path.cwd() == cwd                                       # le dossier courant est restitue
    assert {name: (ROOT / name).exists() for name in repo_before} == repo_before   # le depot n'a pas bougé
    assert info["channel"] == "ma_chaine" and info["account"] == "mon_compte"
    states = {p.parent.name: json.loads(p.read_text(encoding="utf-8"))["status"] for p in (root / "workspace").glob("*/pipeline.json")}
    assert sorted(states.values()) == ["awaiting_review", "done", "running"]       # 3 videos dont une en cours
    clips = list((root / "output").glob("*/*.mp4"))
    assert len(clips) >= 5 and all(c.with_suffix(".json").is_file() for c in clips)
    moments = json.loads((root / "workspace" / "demo0001" / "moments.json").read_text(encoding="utf-8"))
    assert all("jury" in m for m in moments["moments"])                           # moments.json avec jury
    publish = json.loads((root / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    assert {"scheduled", "published", "failed"} <= {e["status"] for e in publish}   # publication programmee incluse
    assert len(list((root / "state" / "stats" / "tiktok" / "mon_compte").glob("*.json"))) >= 5   # historique de stats

    # aucun nom reel ni secret dans ce que le script ecrit
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in (".json", ".toml", ".jsonl"):
            text = path.read_text(encoding="utf-8").lower()
            for token in (*FORBIDDEN_NAMES, *(t.lower() for t in LEAKED_TOKENS), demo.PASSWORD_PLACEHOLDER):
                assert token not in text, f"{token!r} dans {path.relative_to(root)}"
            assert not sensitive_hits(text), f"{path.relative_to(root)} : {sensitive_hits(text)}"
    accounts = json.loads((root / "state" / "accounts.json").read_text(encoding="utf-8"))["accounts"]
    assert all("password" not in a or a["password"] in (None, "") for a in accounts)


# --------------------------------------------------------------------------
# TASK-8c49973515c4 : README et CHANGELOG de la répartition automatique (SPEC-78dc R9-R10)
# --------------------------------------------------------------------------

CHANGELOG = ROOT / "CHANGELOG.md"


def _repartition_section() -> str:
    """Sous-section « Plan de demain » du README, jusqu'au titre suivant de même niveau ou plus haut."""
    text = _readme_text()
    start = text.index("### Plan de demain")
    body = text.index("\n", start) + 1
    following = re.search(r"^#{1,3}\s", text[body:], flags=re.MULTILINE)
    return text[start:body + following.start()] if following else text[start:]


def _unreleased_changelog() -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    start = text.index("## [Non publié]")
    following = re.search(r"^## \[", text[start + 1:], flags=re.MULTILINE)
    return text[start:start + 1 + following.start()] if following else text[start:]


def test_readme_documents_the_plan_de_demain_section():
    section = _repartition_section()
    for needle in ("Plan de demain", "compute_time", "20:00", "Recalculer", "Valider le plan", "Publication"):
        assert needle in section, f"« {needle} » absent de la section Plan de demain"


def test_readme_plan_de_demain_says_nothing_is_sent_without_a_click():
    section = _repartition_section()
    assert re.search(r"rien\s+n'est\s+(créé|cr[ée]e|envoy)", section, flags=re.IGNORECASE), (
        "la règle « rien ne part sans clic » manque")
    assert "clic" in section


def test_readme_plan_de_demain_states_the_unknown_game_rule():
    assert "jeu inconnu : regroupé par VOD" in _repartition_section()


def test_readme_repartition_defaults_match_config_defaults():
    """Les défauts cités dans le README sont ceux du dict CONFIG_DEFAULTS de clipper/repartition.py."""
    import json

    repartition = _load_module_from_clipper("repartition")
    rows = re.findall(r"^\|\s*`([a-z_]+)`\s*\|\s*`([^`]*)`\s*\|", _repartition_section(), flags=re.MULTILINE)
    cited = {key: json.loads(default) for key, default in rows}
    assert set(cited) == set(repartition.CONFIG_DEFAULTS), (
        f"clés citées et clés réelles diffèrent : {sorted(set(cited) ^ set(repartition.CONFIG_DEFAULTS))}")
    for key, default in cited.items():
        assert default == repartition.CONFIG_DEFAULTS[key], f"[repartition] {key} : README {default!r}, code {repartition.CONFIG_DEFAULTS[key]!r}"


def test_changelog_unreleased_lists_the_5_of_5_repartition_entry():
    unreleased = _unreleased_changelog()
    marker = "Répartition automatique (5/5, SPEC-78dc"
    assert marker in unreleased
    assert "README" in unreleased.split(marker, 1)[1].split("\n", 1)[0]


def test_changelog_unreleased_says_the_bonus_min_age_changed():
    unreleased = _unreleased_changelog()
    assert "bonus_min_age_h" in unreleased and "Modifié" in unreleased


def _load_module_from_clipper(name: str):
    sys.path.insert(0, str(ROOT))
    try:
        return importlib.import_module(f"clipper.{name}")
    finally:
        sys.path.remove(str(ROOT))
