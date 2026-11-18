"""The docs stay usable: links work, the README stays short, Mermaid blocks are closed, the
guides keep their sections, and nothing machine- or security-specific leaks into them.

These tests check structure, not truth: reviews still check that the content matches the code.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "docs" / "architecture.md"
RUNBOOK = ROOT / "docs" / "runbook.md"
DOCS = [README, ARCHITECTURE, RUNBOOK]
README_MAX_LINES = 120

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

REQUIRED_HEADINGS = {
    ARCHITECTURE: [
        "## Overview",
        "### Components now",
        "### Planned data flow",
        "## Configuration and profiles",
        "## Where data lives",
        "## Security model",
        "### Two logins per database",
        "### TLS refusal",
        "### Secrets, masking and scanning",
        "### Public repo and SEBI rules",
        "## Databases",
        "## Postgres schema v1",
        "## MongoDB",
        "## Logging and alerts",
        "## Health check",
        "## Compute and Colab",
        "## Testing and CI",
        "## Planned (later phases)",
        *[f"### Phase {n}" for n in range(1, 9)],
    ],
    RUNBOOK: [
        "## Daily routine",
        "## Reading the health output",
        "## Setting up a new Windows laptop",
        "### Main laptop and Docker",
        "## Secrets",
        "## Migrations",
        "## Adding a MongoDB collection",
        "## Common problems",
        "## Changing the project",
    ],
}


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation removed, spaces to hyphens."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    in_code, found = False, set()
    for line in text(path).splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("#"):
            found.add(slug(line.lstrip("#")))
    return found


def relative_links(path: Path) -> list[str]:
    return [
        link
        for link in LINK.findall(text(path))
        if not re.match(r"^[a-z][a-z0-9+.-]*:", link)  # http:, https:, mailto:
    ]


def test_readme_links_the_docs_and_license():
    links = relative_links(README)
    for target in ("docs/architecture.md", "docs/runbook.md", "docs/decisions.md", "LICENSE.md"):
        assert any(link.split("#")[0] == target for link in links), f"README misses {target}"
        assert (ROOT / target).is_file(), f"{target} does not exist"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_relative_links_point_to_existing_files_and_headings(doc):
    for link in relative_links(doc):
        target, _, anchor = link.partition("#")
        path = (doc.parent / target).resolve() if target else doc
        assert path.exists(), f"{doc.name}: broken link {link}"
        if anchor and path.suffix == ".md":
            assert anchor in anchors(path), f"{doc.name}: no heading for #{anchor} in {path.name}"


def test_readme_is_short():
    lines = text(README).splitlines()
    assert len(lines) <= README_MAX_LINES, f"README has {len(lines)} lines"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_mermaid_blocks_are_closed(doc):
    open_block = None
    for number, line in enumerate(text(doc).splitlines(), start=1):
        stripped = line.strip()
        if open_block is None and stripped == "```mermaid":
            open_block = number
        elif open_block is not None and stripped.startswith("```"):
            assert stripped == "```", f"{doc.name}: mermaid block from line {open_block} not closed"
            open_block = None
    assert open_block is None, f"{doc.name}: mermaid block from line {open_block} not closed"


def test_architecture_has_both_diagrams():
    assert text(ARCHITECTURE).count("```mermaid") == 2


@pytest.mark.parametrize("doc", list(REQUIRED_HEADINGS), ids=lambda p: p.name)
def test_required_headings(doc):
    lines = {line.rstrip() for line in text(doc).splitlines()}
    missing = [h for h in REQUIRED_HEADINGS[doc] if h not in lines]
    assert not missing, f"{doc.name} misses headings: {missing}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_no_windows_absolute_paths(doc):
    found = re.findall(r"\b[A-Za-z]:\\", text(doc))
    assert not found, f"{doc.name}: use a placeholder like <repo folder>, not {found}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_no_hosts_or_email_addresses(doc):
    content = text(doc)
    assert not re.search(r"\.neon\.tech|\.mongodb\.net|ep-[a-z]+-[a-z]+-", content), doc.name
    assert not re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", content), doc.name


def test_readme_keeps_the_disclaimer_block():
    assert text(README).splitlines()[2].startswith("> Personal learning project.")
