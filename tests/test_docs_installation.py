"""TASK-1b5a73188dba : documentation de l'installeur portable (SPEC-38f7761891f6
R10). Verifie mecaniquement ce qui peut l'etre : sections de docs/INSTALLATION.md
dans l'ordre du critere, chemins par defaut cites, section README et entree
CHANGELOG. Le contenu redactionnel fin se lit a l'oeil."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLATION = ROOT / "docs" / "INSTALLATION.md"
README = ROOT / "README.md"
VERSIONS = ROOT / "docs" / "versions.md"
CHANGELOG = ROOT / "CHANGELOG.md"

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


REQUIRED_SECTIONS_IN_ORDER = [
    "## Prérequis",
    "## Télécharger",
    "## Installer.bat",
    "## Connexion à Claude",
    "## Où sont le programme et les données",
    "## Premier clip",
    "## GPU",
    "## Mise à jour",
    "## Désinstallation",
    "## Diagnostic",
    "## Problèmes fréquents",
]

REQUIRED_TROUBLESHOOTING_ITEMS = [
    "claude",
    "Chrome",
    "8000",
    "SmartScreen",
]

DEFAULT_APP_PATH = r"%LOCALAPPDATA%\Clipper\app"
DEFAULT_DATA_PATH = r"Documents\Clipper"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_installation_doc_exists():
    assert INSTALLATION.is_file()


def test_installation_doc_has_all_sections_in_order():
    text = _text(INSTALLATION)
    positions = []
    for needle in REQUIRED_SECTIONS_IN_ORDER:
        index = text.find(needle)
        assert index != -1, f"section manquante dans docs/INSTALLATION.md : {needle!r}"
        positions.append(index)
    assert positions == sorted(positions), "sections de docs/INSTALLATION.md pas dans l'ordre attendu"


def test_installation_doc_mentions_both_default_paths():
    text = _text(INSTALLATION)
    assert DEFAULT_APP_PATH in text, "chemin par defaut du programme absent de docs/INSTALLATION.md"
    assert DEFAULT_DATA_PATH in text, "chemin par defaut des donnees absent de docs/INSTALLATION.md"


def test_installation_doc_prerequisites_mention_windows_claude_and_chrome():
    text = _text(INSTALLATION)
    start = text.index("Prérequis")
    end = text.index("Télécharger")
    section = text[start:end]
    for needle in ("Windows 10", "64 bits", "Claude", "Chrome"):
        assert needle in section, f"prerequis manquant : {needle!r}"


def test_installation_doc_troubleshooting_section_covers_required_cases():
    text = _text(INSTALLATION)
    start = text.index("Problèmes fréquents")
    section = text[start:]
    for needle in REQUIRED_TROUBLESHOOTING_ITEMS:
        assert needle in section, f"cas manquant dans Problemes frequents : {needle!r}"


def test_installation_doc_has_no_real_person_or_channel_name():
    text = _text(INSTALLATION)
    assert not sensitive_hits(text), f"docs/INSTALLATION.md : {sensitive_hits(text)}"


def test_readme_has_no_dev_tools_installation_section_before_developer_one():
    text = _text(README)
    no_dev_index = text.index("Installation sans outils de développement")
    dev_index = text.index("Installation développeur")
    assert no_dev_index < dev_index


def test_readme_no_dev_tools_section_links_to_installation_doc():
    text = _text(README)
    start = text.index("Installation sans outils de développement")
    end = text.index("Installation développeur")
    section = text[start:end]
    assert "docs/INSTALLATION.md" in section


def test_readme_developer_installation_content_is_unchanged():
    text = _text(README)
    start = text.index("## Installation développeur")
    end = text.index("## Démarrage rapide")
    section = text[start:end]
    for needle in ("uv venv", 'uv pip install -e ".[test]"', "tools/setup.ps1"):
        assert needle in section


def test_versions_doc_criterion_one_is_about_the_portable_zip():
    text = _text(VERSIONS)
    match = re.search(r"1\. \*\*[^*]+\*\* ?:? ?(.+)", text)
    assert match, "critere n1 de la v1.0.0 introuvable"
    criterion = match.group(1)
    assert "zip portable" in criterion
    assert "Release" in criterion
    assert "premier clip" in criterion
    assert "sans aide" in criterion


def test_m6_installation_doc_diagnostic_section_has_no_phantom_menu():
    """M6 (TASK-4f1d7d1ee341) : la section Diagnostic ne renvoie plus vers un
    bouton "menu Reglages" de la console, qui n'existe pas (clipper/web/
    n'a aucun appel a doctor)."""
    text = _text(INSTALLATION)
    start = text.index("## Diagnostic")
    end = text.index("## Problèmes fréquents")
    section = text[start:end]
    assert "menu" not in section.lower()


def test_changelog_current_version_has_an_entry_about_the_installer():
    text = _text(CHANGELOG)
    start = text.index("## [0.4.1]")
    end = text.index("## [0.4.0]")
    section = text[start:end]
    assert section.strip() != "", "section [0.4.1] vide"
    for needle in ("Installer.bat", "portable"):
        assert needle in section, f"{needle!r} absent de l'entree [0.4.1] du CHANGELOG"
