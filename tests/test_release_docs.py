"""TASK-c60110ce236a puis TASK-492458dfe349 : publication d'une pre-version. Le
fond redactionnel se relit a l'oeil ; ce fichier verifie mecaniquement ce qui
peut l'etre : version du paquet, structure Keep a Changelog, notes de version,
plan de versions, absence de noms reels. Aucune version n'est ecrite en dur :
la version courante est lue dans pyproject.toml."""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
VERSIONS = ROOT / "docs" / "versions.md"
PYPROJECT = ROOT / "pyproject.toml"
README = ROOT / "README.md"
REPO_URL = "https://github.com/ZeDKouill3/TiktokClipper"

# Identifiants, noms de personnes ou de chaines reels (research/, hors scope git) :
# ne doivent jamais fuiter dans un artefact versionne.
LEAKED_TOKENS = ("7VaA8XUKrAY", "ivl0nxa3C7o", "C:\\Users")

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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _current_version() -> str:
    return tomllib.loads(_read(PYPROJECT))["project"]["version"]


VERSION = _current_version()
TAG = f"v{VERSION}"
ESC_VERSION = re.escape(VERSION)
NOTES = ROOT / "docs" / "releases" / f"{TAG}.md"
HAS_NOTES = NOTES.exists()
NO_NOTES_REASON = f"{TAG} n'a pas de fichier de notes séparé : le changelog suffit"


def _next_minor(version: str) -> str:
    major, minor, _patch = version.split(".")
    return f"{major}.{int(minor) + 1}.0"


NEXT_VERSION = _next_minor(VERSION)


def _changelog_section(text: str, title_pattern: str) -> str:
    match = re.search(rf"^## {title_pattern}.*?$(.*?)(?=^## |^\[[^\]]+\]: |\Z)", text, re.S | re.M)
    assert match, f"section {title_pattern!r} absente de CHANGELOG.md"
    return match.group(1)


def test_package_version_is_a_plain_semver_and_matches_the_module():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION), VERSION
    init = _read(ROOT / "clipper" / "__init__.py")
    match = re.search(r'^__version__ = "([^"]+)"$', init, re.M)
    assert match, "clipper/__init__.py n'expose pas __version__"
    assert match.group(1) == VERSION


def test_clipper_version_attribute_matches_pyproject():
    import clipper

    assert clipper.__version__ == VERSION


def test_readme_version_badge_and_notes_link_follow_the_current_version():
    text = _read(README)
    assert f"version-{VERSION}" in text
    if HAS_NOTES:
        assert f"docs/releases/{TAG}.md" in text


def test_changelog_header_mentions_semantic_versioning():
    head = _read(CHANGELOG).split("## ", 1)[0]
    assert "versionnage sémantique" in head
    assert "docs/versions.md" in head
    assert "pas (encore)" not in head


def _published_headings(text: str) -> list[str]:
    return [h for h in re.findall(r"^## (.+)$", text, re.M) if h != "[Non publié]"]


def test_changelog_has_unreleased_then_the_current_version_first():
    text = _read(CHANGELOG)
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert headings[0] == "[Non publié]"
    assert re.fullmatch(rf"\[{ESC_VERSION}\] - \d{{4}}-\d{{2}}-\d{{2}}", headings[1]), headings[1]
    # [Non publié] recoit les changements entre deux versions : vide, ou rangé en sections Keep a Changelog.
    unreleased = _changelog_section(text, r"\[Non publié\]").strip()
    if unreleased:
        titles = re.findall(r"^### (.+)$", unreleased, re.M)
        assert titles and unreleased.startswith("### "), "[Non publié] non vide : contenu hors section ###"
        assert set(titles) <= {
            "Ajouté", "Modifié", "Corrigé", "Sécurité", "Retiré", "Obsolète", "À savoir pour migrer",
        }, titles
    assert _published_headings(text)[0].startswith(f"[{VERSION}]")
    assert headings[-1].startswith("[0.1.0]")


def test_changelog_current_version_has_only_non_empty_keep_a_changelog_sections():
    body = _changelog_section(_read(CHANGELOG), rf"\[{ESC_VERSION}\]")
    titles = re.findall(r"^### (.+)$", body, re.M)
    assert titles, f"aucune section ### dans [{VERSION}]"
    assert set(titles) <= {
        "Ajouté", "Modifié", "Corrigé", "Sécurité", "Retiré", "Obsolète", "À savoir pour migrer",
    }, titles
    assert len(titles) == len(set(titles)), titles
    for title in titles:
        block = re.search(rf"^### {title}$(.*?)(?=^### |\Z)", body, re.S | re.M)
        assert block.group(1).strip(), f"### {title} vide dans [{VERSION}]"


@pytest.mark.parametrize("section", ["Ajouté", "Modifié", "Corrigé", "Sécurité", "Retiré"])
def test_changelog_0_2_0_keeps_each_keep_a_changelog_section(section):
    body = _changelog_section(_read(CHANGELOG), r"\[0\.2\.0\]")
    block = re.search(rf"^### {section}$(.*?)(?=^### |\Z)", body, re.S | re.M)
    assert block, f"### {section} absente de [0.2.0]"
    assert block.group(1).strip(), f"### {section} vide"


def test_changelog_footer_links_compare_consecutive_tags():
    text = _read(CHANGELOG)
    versions = [re.match(r"\[([^\]]+)\]", h).group(1) for h in _published_headings(text)]
    assert versions[0] == VERSION
    assert f"[Non publié]: {REPO_URL}/compare/{TAG}...HEAD" in text
    for newer, older in zip(versions, versions[1:]):
        assert f"[{newer}]: {REPO_URL}/compare/v{older}...v{newer}" in text, newer
    assert f"[{versions[-1]}]: {REPO_URL}/releases/tag/v{versions[-1]}" in text


@pytest.mark.skipif(not HAS_NOTES, reason=NO_NOTES_REASON)
def test_changelog_current_version_points_to_its_release_notes():
    body = _changelog_section(_read(CHANGELOG), rf"\[{ESC_VERSION}\]")
    assert f"docs/releases/{TAG}.md" in body


REQUIRED_NOTE_THEMES = [
    "Console web", "Chaînes", "Comptes et coffre", "Publication TikTok", "Statistiques TikTok",
    "Jury et grille gaming", "Worker et file", "Performances",
]


@pytest.mark.skipif(not HAS_NOTES, reason=NO_NOTES_REASON)
def test_release_notes_have_the_expected_structure():
    text = _read(NOTES)
    headings = re.findall(r"^#{1,3} (.+)$", text, re.M)
    previous = _published_headings(_read(CHANGELOG))[1]
    previous_version = re.match(r"\[([^\]]+)\]", previous).group(1)
    for section in ("Points forts", f"Mettre à jour depuis la {previous_version}", "Avertissements", "Limites connues", "Remerciements"):
        assert any(section in h for h in headings), f"section manquante : {section}"
    assert text.startswith(f"# clipper {TAG}")


@pytest.mark.skipif(not HAS_NOTES, reason=NO_NOTES_REASON)
def test_release_notes_cover_the_upgrade_steps():
    text = _read(NOTES)
    for needle in ("uv pip install", "config.toml", "[parts]", "rubric_path", "stats_interval_h",
                   "réseau non résidentiel", "cookies", "captcha"):
        assert needle in text, f"manque dans les notes : {needle}"


@pytest.mark.skipif(not HAS_NOTES, reason=NO_NOTES_REASON)
def test_release_notes_upgrade_section_names_the_removed_parts_key_and_the_stats_default():
    text = _read(NOTES)
    upgrade = re.search(r"^## Mettre à jour depuis la .*?$(.*?)(?=^## )", text, re.S | re.M)
    assert upgrade, "section « Mettre à jour » absente"
    body = upgrade.group(1)
    assert "[parts]" in body and "rubric_path" in body and "stats_interval_h" in body


@pytest.mark.skipif(not HAS_NOTES, reason=NO_NOTES_REASON)
def test_release_notes_list_artifacts_of_the_current_version():
    text = _read(NOTES)
    assert f"clipper-{VERSION}-py3-none-any.whl" in text
    assert f"clipper-{VERSION}.tar.gz" in text
    assert f"/blob/{TAG}/" in text


@pytest.mark.skipif(not HAS_NOTES, reason=NO_NOTES_REASON)
def test_release_notes_use_a_neutral_channel_example():
    assert "ma_chaine" in _read(NOTES)


def test_0_2_0_release_notes_are_kept_with_their_themes():
    text = _read(ROOT / "docs" / "releases" / "v0.2.0.md")
    headings = re.findall(r"^#{1,3} (.+)$", text, re.M)
    for theme in REQUIRED_NOTE_THEMES:
        assert any(theme in h for h in headings), f"thème manquant : {theme}"
    assert text.startswith("# clipper v0.2.0")


_LEAK_CHECKED_PATHS = [CHANGELOG, VERSIONS, README] + ([NOTES] if HAS_NOTES else [])


@pytest.mark.parametrize("path", _LEAK_CHECKED_PATHS, ids=lambda p: p.name)
def test_release_docs_leak_no_real_identifier(path):
    text = _read(path)
    for token in LEAKED_TOKENS:
        assert token not in text, f"{token!r} dans {path.name}"
    assert not sensitive_hits(text), f"{path.name} : {sensitive_hits(text)}"


def test_versions_marks_the_current_version_published_with_its_content():
    lines = _read(VERSIONS).splitlines()
    row = next(line for line in lines if line.startswith(f"| {TAG} "))
    assert row.rstrip().endswith("| publiée |")
    if HAS_NOTES:
        assert f"releases/{TAG}.md" in row
    else:
        assert "CHANGELOG.md" in row
    assert "maintenant" not in row
    assert "à définir" not in row.lower()


def test_versions_published_rows_are_never_marked_planned():
    lines = [line for line in _read(VERSIONS).splitlines() if re.match(r"\| v\d", line)]
    published = [line for line in lines if line.rstrip().endswith("| publiée |")]
    assert published and f"| {TAG} " in published[-1]
    for line in lines[len(published):]:
        assert not line.rstrip().endswith("| publiée |"), line


def test_versions_keeps_the_next_version_after_real_usage():
    text = _read(VERSIONS)
    row = next(line for line in text.splitlines() if line.startswith(f"| v{NEXT_VERSION} "))
    assert "Mode auto" in row and row.rstrip().endswith("| après une semaine d'usage réel |")


def test_readme_wheel_example_and_section_link_follow_the_current_version():
    text = _read(README)
    assert f"clipper-{VERSION}-py3-none-any.whl" in text
    assert f"(section `[{VERSION}]`)" in text
    assert "0.4.0-py3-none" not in text


def test_readme_portable_zip_points_to_the_release_with_the_current_version():
    text = _read(README)
    start = text.index("## Installation sans outils de développement")
    end = text.index("## Installation développeur")
    section = text[start:end]
    assert f"Clipper-portable-{VERSION}.zip" in section
    assert f"{REPO_URL}/releases" in section
    assert "<version>" not in section


def test_versions_row_for_the_current_version_names_the_release_headline():
    lines = _read(VERSIONS).splitlines()
    row = next(line for line in lines if line.startswith(f"| {TAG} "))
    assert "Veille" in row


REQUIRED_CHANGELOG_TERMS = (
    f"Clipper-portable-{VERSION}.zip", "Installer.bat", "mise à jour", "Veille", "Fiche par clip",
    "Préchargement du téléchargement", "prefetch_download", "concurrent_fragments", "extract_batch",
    "facecam_clip_face_margin", "click_timeout_s", "zero_view_alert_hours", "[action]", "twitch_access_workers",
)


@pytest.mark.parametrize("term", REQUIRED_CHANGELOG_TERMS)
def test_changelog_current_version_covers_the_release_changes(term):
    body = _changelog_section(_read(CHANGELOG), rf"\[{ESC_VERSION}\]")
    assert term in body, f"{term!r} absent de la section [{VERSION}]"


def test_changelog_current_version_has_no_separate_release_notes_file():
    assert not NOTES.exists(), f"{NOTES.name} : le changelog suffit pour {TAG}"
