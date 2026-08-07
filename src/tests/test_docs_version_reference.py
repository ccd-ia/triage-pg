"""The published docs must not advertise a version the package no longer is.

The site drifted twice before this guard existed: the landing page told readers to
``docker pull …:v1.0.0-rc2`` two releases after ``v1.0.0`` shipped, and two pages printed
``triage-pg 1.0.0`` as captured CLI output. Both are the kind of error nobody notices from
the inside — the reader is the one who hits it.

Then the same defect recurred *outside* the guard's reach: ``README.md`` — the repo's
front page — advertised ``:v1.0.0-rc1`` three releases after that tag, because the scan
root here was ``docs-site/`` only. The repo-root markdown is now scanned too; the guard's
blast radius is a constant in this file, and narrowing it is how drift survives.

The banner in ``docs-site/astro.config.mjs`` derives the version from ``pyproject.toml`` at
build time and cannot rot. Prose and captured console output can, so they are checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

import triage

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_SITE = REPO_ROOT / "docs-site" / "src" / "content" / "docs"
#: Repo-root pages readers hit before the docs site — the README rc1 drift lived here.
ROOT_DOCS = (REPO_ROOT / "README.md", REPO_ROOT / "CONTRIBUTING.md")
ASTRO_CONFIG = REPO_ROOT / "docs-site" / "astro.config.mjs"
VERSION_BANNER = REPO_ROOT / "docs-site" / "src" / "components" / "VersionBanner.astro"


def _scanned_files() -> list[Path]:
    """Every file the version guards read: the docs site plus the repo-root pages."""
    return [*sorted(DOCS_SITE.rglob("*.md*")), *ROOT_DOCS]


#: ``triage --version`` output captured into a fenced console block.
_CLI_VERSION = re.compile(r"^triage-pg (\d+\.\d+\.\d+)", re.MULTILINE)
#: A pinned image tag in a pull command, e.g. ``ghcr.io/ccd-ia/triage-pg:v1.1.0``.
_IMAGE_TAG = re.compile(r"ghcr\.io/ccd-ia/triage-pg:v(\d+\.\d+\.\d+)(-rc\d+)?")


def test_docs_site_exists():
    """Guard the guard: a moved docs tree must fail loudly, not silently pass."""
    assert DOCS_SITE.is_dir(), f"docs site content not found at {DOCS_SITE}"
    assert ASTRO_CONFIG.is_file(), f"astro config not found at {ASTRO_CONFIG}"
    for path in ROOT_DOCS:
        assert path.is_file(), (
            f"repo-root page not found at {path} — if it was renamed, update ROOT_DOCS"
            " so the version guards keep scanning it"
        )


def test_captured_cli_version_matches_the_package():
    stale: list[str] = []
    for path in _scanned_files():
        for match in _CLI_VERSION.finditer(path.read_text(encoding="utf-8")):
            if match.group(1) != triage.__version__:
                stale.append(
                    f"{path.relative_to(REPO_ROOT)}: 'triage-pg {match.group(1)}'"
                )
    assert not stale, (
        f"docs show a CLI version other than {triage.__version__}: {stale}."
        " Re-capture the output, or bump the docs alongside the release."
    )


def test_advertised_image_tag_matches_the_package():
    stale: list[str] = []
    for path in _scanned_files():
        for match in _IMAGE_TAG.finditer(path.read_text(encoding="utf-8")):
            tag = match.group(1) + (match.group(2) or "")
            if tag != triage.__version__:
                stale.append(f"{path.relative_to(REPO_ROOT)}: ':v{tag}'")
    assert not stale, (
        f"docs advertise an image tag other than v{triage.__version__}: {stale}."
        " A reader following these pages would pull the wrong image."
    )


def test_version_banner_is_derived_not_hardcoded():
    """The banner must read pyproject.toml — a literal there would drift like the rest."""
    assert VERSION_BANNER.is_file(), (
        f"version banner component missing at {VERSION_BANNER}"
    )
    banner = VERSION_BANNER.read_text(encoding="utf-8")
    assert "pyproject.toml" in banner, (
        "the docs-site version banner no longer reads pyproject.toml; a hardcoded"
        " version will silently go stale on the next release"
    )
    assert re.search(r"\{version\}", banner), (
        "the banner should render the derived version, not a literal"
    )
    # ...and it must actually be wired in, or it renders on no page at all.
    config = ASTRO_CONFIG.read_text(encoding="utf-8")
    assert "VersionBanner.astro" in config, (
        "VersionBanner.astro exists but is not registered as Starlight's Banner override"
        " in astro.config.mjs — it would never render"
    )
