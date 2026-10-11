"""Vendoring de Basecoat et Idiomorph dans clipper/web/static/vendor/ (ADR-a308).

    python tools/vendor_basecoat.py          télécharge les tarballs npm épinglés,
                                             préfixe le CSS en `.v3`, écrit VERSIONS
    python tools/vendor_basecoat.py --check  vérifie les sha256 de VERSIONS, hors réseau

Le script ne tourne jamais à l'installation ni au lancement : son résultat est commité.
Une erreur d'intégrité ou un at-rule inconnu lève une exception (ADR-ad2e).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "clipper" / "web" / "static" / "vendor"

PREFIX = ".v3"
# Sélecteurs qui restent globaux : racine, thème sombre, élément html.
WHITELIST = frozenset({":root", ".dark", "html"})
# At-rules dont le contenu n'est pas un sélecteur : gardées telles quelles.
KEPT_AT = frozenset({"keyframes", "-webkit-keyframes", "property", "font-face"})
# At-rules conditionnelles ou de cascade : leurs règles internes sont préfixées.
NESTED_AT = frozenset({"media", "supports", "layer", "container", "starting-style"})

PINS = (
    {
        "name": "basecoat-css",
        "version": "1.0.2",
        "licence": "MIT",
        "integrity": "sha512-C6I5rr7HziIAoBYNGPO5U6HaU/6FuF3iVp++IgoEqqJ836is3XoRty6l1ibXqxnoIRZNnOAYDMX8QxVC+58zhw==",
        "files": {
            "package/dist/basecoat.cdn.min.css": "basecoat.cdn.min.css",
            "package/dist/js/all.min.js": "basecoat.all.min.js",
            "package/LICENSE.md": "LICENSE-basecoat.md",
        },
    },
    {
        "name": "idiomorph",
        "version": "0.8.0",
        "licence": "0BSD",
        "integrity": "sha512-R47JBL+6jHDQvcnbppIUtXXcxi5h6+wuCEEZXnRhUltsgzPFKzThL5PuHppP9ypJuy+7WawcY1TsSBD/iQbMhw==",
        "files": {
            "package/dist/idiomorph.min.js": "idiomorph.min.js",
            "package/LICENSE": "LICENSE-idiomorph.txt",
        },
    },
)


# --- découpage CSS -------------------------------------------------------

def _skip_string(css: str, i: int) -> int:
    quote = css[i]
    j = i + 1
    while j < len(css):
        if css[j] == "\\":
            j += 2
            continue
        if css[j] == quote:
            return j + 1
        j += 1
    raise ValueError("chaîne CSS non fermée")


def _block_end(css: str, i: int) -> int:
    """Index de l'accolade fermante qui répond à l'accolade ouvrante en `i`."""
    depth = 0
    j = i
    while j < len(css):
        c = css[j]
        if c in "\"'":
            j = _skip_string(css, j)
            continue
        if css.startswith("/*", j):
            j = css.index("*/", j + 2) + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    raise ValueError("accolade CSS non fermée")


def _has_block(body: str) -> bool:
    """Vrai si une accolade ouvrante apparaît hors chaînes et commentaires."""
    i = 0
    while i < len(body):
        c = body[i]
        if c in "\"'":
            i = _skip_string(body, i)
            continue
        if body.startswith("/*", i):
            i = body.index("*/", i + 2) + 2
            continue
        if c == "{":
            return True
        i += 1
    return False


def _split_top(text: str, sep: str = ",") -> list[str]:
    """Coupe `text` sur `sep` hors parenthèses, crochets, chaînes et échappements."""
    parts: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c in "\"'":
            i = _skip_string(text, i)
            continue
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _walk(css: str):
    """Yields (genre, ...) au premier niveau : comment, statement, block."""
    i = 0
    n = len(css)
    while True:
        while i < n and css[i].isspace():
            i += 1
        if i >= n:
            return
        if css.startswith("/*", i):
            end = css.index("*/", i + 2) + 2
            yield ("comment", css[i:end])
            i = end
            continue
        start = i
        while i < n and css[i] not in "{;":
            i = _skip_string(css, i) if css[i] in "\"'" else i + 1
        if i >= n:
            raise ValueError(f"CSS tronqué après : {css[start:start + 60]!r}")
        prelude = css[start:i].strip()
        if css[i] == ";":
            yield ("statement", prelude)
            i += 1
            continue
        end = _block_end(css, i)
        yield ("block", prelude, css[i + 1 : end])
        i = end + 1


def _at_name(prelude: str) -> str:
    match = re.match(r"@([\w-]+)", prelude)
    return match.group(1) if match else ""


def prefix_css(css: str) -> str:
    """Préfixe chaque sélecteur de style par `.v3 ` ; garde les blocs à part."""
    out: list[str] = []
    for item in _walk(css):
        if item[0] == "comment":
            out.append(item[1])
        elif item[0] == "statement":
            out.append(item[1] + ";")
        else:
            _, prelude, inner = item
            if prelude.startswith("@"):
                name = _at_name(prelude)
                if name in KEPT_AT:
                    out.append(f"{prelude}{{{inner}}}")
                elif name in NESTED_AT:
                    out.append(f"{prelude}{{{prefix_css(inner)}}}")
                else:
                    raise ValueError(f"at-rule inconnue, refusée : {prelude}")
            else:
                if _has_block(inner):
                    raise ValueError(f"règle imbriquée non prise en charge : {prelude}")
                selectors = [s.strip() for s in _split_top(prelude)]
                prefixed = [s if s in WHITELIST else f"{PREFIX} {s}" for s in selectors]
                out.append(",".join(prefixed) + f"{{{inner}}}")
    return "".join(out)


def style_selectors(css: str) -> list[str]:
    """Sélecteurs individuels des règles de style, sans les réécrire."""
    found: list[str] = []
    for item in _walk(css):
        if item[0] != "block":
            continue
        _, prelude, inner = item
        if prelude.startswith("@"):
            if _at_name(prelude) in NESTED_AT:
                found.extend(style_selectors(inner))
            continue
        found.extend(s.strip() for s in _split_top(prelude))
    return found


# --- VERSIONS et contrôle ------------------------------------------------

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(directory: Path) -> list[str]:
    """Problèmes trouvés dans `directory` face à VERSIONS ; liste vide = intact."""
    versions = directory / "VERSIONS"
    if not versions.exists():
        return [f"VERSIONS absent dans {directory}"]
    problems: list[str] = []
    for line in versions.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[0] != "sha256":
            continue
        _, name, digest = parts
        target = directory / name
        if not target.exists():
            problems.append(f"absent : {name}")
        elif _sha256(target) != digest:
            problems.append(f"sha256 différent : {name}")
    return problems


def _fetch(pin: dict) -> dict[str, bytes]:
    url = f"https://registry.npmjs.org/{pin['name']}/-/{pin['name']}-{pin['version']}.tgz"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    expected = pin["integrity"].split("-", 1)[1]
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if actual != expected:
        raise RuntimeError(f"intégrité {pin['name']} {pin['version']} : attendu {expected}, reçu {actual}")
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member, dest in pin["files"].items():
            handle = tar.extractfile(tar.getmember(member))
            if handle is None:
                raise RuntimeError(f"{member} n'est pas un fichier dans {pin['name']}")
            out[dest] = handle.read()
    return out


def build(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["# Vendoring par tools/vendor_basecoat.py (ADR-a308). Ne pas éditer à la main."]
    for pin in PINS:
        lines.append(f"{pin['name']} {pin['version']} {pin['licence']}")
        for dest, content in _fetch(pin).items():
            if dest == "basecoat.cdn.min.css":
                content = prefix_css(content.decode("utf-8")).encode("utf-8")
            (directory / dest).write_bytes(content)
            lines.append(f"sha256 {dest} {_sha256(directory / dest)}")
    (directory / "VERSIONS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="vérifie les sha256 de VERSIONS, sans réseau")
    args = parser.parse_args(argv)
    if args.check:
        problems = check(VENDOR)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    build(VENDOR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
