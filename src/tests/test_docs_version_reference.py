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
import json
import tomllib
from pathlib import Path
from typing import Any, cast

import yaml

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


# --------------------------------------------------------------- citation files
#: The DOI record's metadata. Zenodo reads .zenodo.json in preference to
#: CITATION.cff (it ignores the latter entirely when both exist), but GitHub's
#: "Cite this repository" widget reads CITATION.cff — so both ship, and both
#: carry a version that has to be the released one.
CITATION_CFF = REPO_ROOT / "CITATION.cff"
ZENODO_JSON = REPO_ROOT / ".zenodo.json"
#: Fields Zenodo reads off .zenodo.json; a missing one degrades the record
#: rather than failing the release, which is why a test names them.
_ZENODO_REQUIRED = (
    "upload_type",
    "title",
    "description",
    "creators",
    "license",
    "access_right",
    "version",
    "keywords",
    "related_identifiers",
)


def _cff() -> dict[str, Any]:
    """CITATION.cff as GitHub's citation widget reads it."""
    return cast(dict[str, Any], yaml.safe_load(CITATION_CFF.read_text("utf-8")))


def _zenodo() -> dict[str, Any]:
    """.zenodo.json as Zenodo reads it — the authoritative record here."""
    return cast(dict[str, Any], json.loads(ZENODO_JSON.read_text("utf-8")))


def test_citation_files_exist():
    """Guard the guard: a renamed citation file must fail loudly, not pass."""
    assert CITATION_CFF.is_file(), f"CITATION.cff not found at {CITATION_CFF}"
    assert ZENODO_JSON.is_file(), (
        f".zenodo.json not found at {ZENODO_JSON}. It is deliberate here and"
        " authoritative: CFF 1.2.0 has no contributors field and no role"
        " vocabulary, so the upstream DSSG credit — Rayid Ghani as"
        " ProjectLeader, the team as ProjectMember, isDerivedFrom → dssg/triage"
        " — exists only in this file. Deleting it silently drops all of it"
        " from the DOI record."
    )


def test_citation_versions_match_the_package():
    """A DOI is minted from these files; a stale version is minted permanently.

    CITATION.cff shipped in no release before v1.1.5 and its version said
    1.1.4, which is exactly the drift a released, citable record cannot carry.
    """
    cff = _cff()
    zenodo = _zenodo()
    assert str(cff["version"]) == triage.__version__, (
        f"CITATION.cff says version {cff['version']}, package is"
        f" {triage.__version__} — the citation would name the wrong release"
    )
    assert str(zenodo["version"]) == triage.__version__, (
        f".zenodo.json says version {zenodo['version']}, package is"
        f" {triage.__version__} — the DOI record would name the wrong release"
    )


def test_zenodo_record_carries_every_field_it_consumes():
    """A missing field does not fail the release; it degrades the record."""
    zenodo = _zenodo()
    missing = [f for f in _ZENODO_REQUIRED if not zenodo.get(f)]
    assert not missing, (
        f".zenodo.json is missing fields Zenodo reads: {missing}."
        " The archived record degrades silently without them."
    )
    assert cast(str, zenodo["upload_type"]) == "software"
    without_orcid = [c["name"] for c in zenodo["creators"] if not c.get("orcid")]
    assert not without_orcid, (
        f"creators without an ORCID: {without_orcid}. The ORCID is what ties an"
        " archived release to a person rather than to a name string."
    )


def test_the_two_citation_files_agree():
    """Zenodo reads only .zenodo.json; GitHub's widget reads only CITATION.cff.

    Nothing makes them agree, so they can publish different titles, licences or
    keywords for the same release — the DOI landing page saying one thing and
    the repository's own citation widget another.
    """
    cff = _cff()
    zenodo = _zenodo()
    assert cff["title"] == zenodo["title"], (
        f"titles differ: CITATION.cff {cff['title']!r} vs"
        f" .zenodo.json {zenodo['title']!r}"
    )
    assert cff["license"].lower() == zenodo["license"].lower(), (
        f"licences differ: CITATION.cff {cff['license']!r} vs"
        f" .zenodo.json {zenodo['license']!r}"
    )
    assert list(cff["keywords"]) == list(zenodo["keywords"]), (
        "keywords differ between CITATION.cff and .zenodo.json"
    )
    assert cff.get("abstract"), (
        "CITATION.cff has no abstract; GitHub's citation widget and any CFF"
        " consumer would show the title alone"
    )


def test_upstream_credit_survives_in_the_zenodo_record():
    """The DSSG attribution is the whole reason .zenodo.json exists here.

    It is a public credit decision, not a formatting one: dropping the role or
    the derivation link turns a credited rewrite back into an uncredited one.
    """
    zenodo = _zenodo()
    leaders = [
        c["name"] for c in zenodo["contributors"] if c["type"] == "ProjectLeader"
    ]
    assert leaders == ["Ghani, Rayid"], (
        f"expected Rayid Ghani as the record's ProjectLeader, found {leaders}."
        " He originated and led DSSG triage, which this project derives from."
    )
    members = {c["name"] for c in zenodo["contributors"]}
    for name in ("Millan, Liliana", "Amarasinghe, Kasun"):
        assert name in members, (
            f"{name} is missing from the contributors. Upstream's own"
            " AUTHORS.rst omits them because they contributed after it was"
            " last touched; that omission is corrected here on purpose."
        )
    derived = [
        r["identifier"]
        for r in zenodo["related_identifiers"]
        if r["relation"] == "isDerivedFrom"
    ]
    assert derived == ["https://github.com/dssg/triage"], (
        f"expected an isDerivedFrom relation to dssg/triage, found {derived}."
        " That relation is what makes the lineage machine-readable."
    )


def test_package_version_matches_pyproject():
    """`triage --version` prints ``__version__``; the release guard reads pyproject.

    Nothing compared the two, so a bump to one alone would publish an image
    whose ``triage --version`` contradicts its own tag — the exact failure the
    release workflow's guard exists to prevent, reached through the other file.
    """
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["version"] == triage.__version__, (
        f"pyproject.toml says {pyproject['project']['version']},"
        f" triage.__version__ says {triage.__version__}"
    )
