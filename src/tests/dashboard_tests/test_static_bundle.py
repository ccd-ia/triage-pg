"""The dashboard SPA ships in the wheel, and says so loudly when it does not (#15).

`packages = ["src/triage"]` shipped `triage/dashboard/static/` holding only a 620-byte
placeholder, so `uvicorn triage.dashboard.app:app` from a git or wheel install returned HTTP 200
for every route with a blank-looking page. `_SpaStaticFiles` falls back to `index.html` for any
non-`/api` path, so the 200 is by design — which is exactly why the empty dashboard read as "my
data is missing" rather than "no SPA was built".

The build half is tested by actually building a wheel, twice: once with a bundle to include and
once with neither a bundle nor npm. The second case must SUCCEED — a machine with no node
toolchain has to keep being able to install triage.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_packaged_placeholder_is_detectable() -> None:
    """The sentinel, not a string in the HTML — the page's wording is documentation."""
    from triage.dashboard.app import is_placeholder_static

    packaged = REPO_ROOT / "src" / "triage" / "dashboard" / "_placeholder"
    assert packaged.is_dir()
    assert is_placeholder_static(packaged), (
        "the packaged static dir must carry the sentinel, or the startup warning can never fire"
    )


def test_a_real_bundle_is_not_flagged(tmp_path: Path) -> None:
    from triage.dashboard.app import is_placeholder_static

    built = tmp_path / "dist"
    built.mkdir()
    (built / "index.html").write_text(
        "<!doctype html><script src='/assets/app.js'></script>"
    )
    assert not is_placeholder_static(built)


def test_the_placeholder_page_names_the_commands_that_fix_it() -> None:
    """It used to read as an internal note ('wired at integration'), which helps nobody."""
    page = (
        REPO_ROOT / "src" / "triage" / "dashboard" / "_placeholder" / "index.html"
    ).read_text()
    assert "npm ci" in page and "npm run build" in page
    assert "TRIAGE_DASHBOARD_STATIC" in page


# --------------------------------------------------------------------- the build hook


def _build_wheel(source: Path, dest: Path, env: dict[str, str]) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH — cannot build a wheel here")
    result = subprocess.run(  # noqa: S603 — uv off PATH, no shell
        [uv, "build", "--wheel", "--out-dir", "dist"],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    wheels = sorted((source / "dist").glob("*.whl"))
    assert result.returncode == 0, (
        f"the wheel build FAILED (exit {result.returncode}) — a machine without node must"
        f" still be able to install triage\n{result.stdout}\n{result.stderr}"
    )
    assert wheels, f"no wheel produced\n{result.stdout}\n{result.stderr}"
    target = dest / wheels[-1].name
    shutil.copy(wheels[-1], target)
    return target


def _static_members(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return [n for n in archive.namelist() if "/dashboard/" in n]


@pytest.fixture
def minimal_source(tmp_path: Path) -> Path:
    """A throwaway package tree wearing the same build hook, so the test builds in seconds."""
    source = tmp_path / "pkg"
    (source / "src" / "triage" / "dashboard" / "_placeholder").mkdir(parents=True)
    (source / "src" / "triage" / "__init__.py").write_text("")
    (source / "src" / "triage" / "dashboard" / "__init__.py").write_text("")
    placeholder = source / "src" / "triage" / "dashboard" / "_placeholder"
    (placeholder / "index.html").write_text("<html>placeholder</html>")
    (placeholder / ".triage-placeholder").write_text("placeholder\n")
    shutil.copy(REPO_ROOT / "build_frontend.py", source / "build_frontend.py")
    (source / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling>=1.25"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[project]\n"
        'name = "triage"\n'
        'version = "0.0.0"\n\n'
        "[tool.hatch.build.targets.wheel]\n"
        'packages = ["src/triage"]\n\n'
        "[tool.hatch.build.targets.wheel.hooks.custom]\n"
        'path = "build_frontend.py"\n'
    )
    return source


def test_a_prebuilt_dist_is_included_in_the_wheel(
    minimal_source: Path, tmp_path: Path
) -> None:
    """CI and the Dockerfile's frontend-build stage both leave a dist; it must be reused."""
    dist = minimal_source / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><script src='/assets/app.js'></script>"
    )
    (dist / "assets" / "app.js").write_text("console.log('spa')")

    wheel = _build_wheel(minimal_source, tmp_path, {**os.environ})
    members = _static_members(wheel)

    assert any(m.endswith("/dashboard/static/assets/app.js") for m in members), (
        f"the built SPA is missing from the wheel: {members}"
    )
    with zipfile.ZipFile(wheel) as archive:
        index = next(m for m in members if m.endswith("/dashboard/static/index.html"))
        assert b"<script" in archive.read(index), (
            "the wheel's index.html is still the placeholder, not the built SPA"
        )


def test_no_bundle_and_no_npm_still_produces_an_installable_wheel(
    minimal_source: Path, tmp_path: Path
) -> None:
    """The decisive case: a node-less machine must not lose the ability to install."""
    env = {**os.environ, "TRIAGE_SKIP_FRONTEND_BUILD": "1"}

    wheel = _build_wheel(minimal_source, tmp_path, env)
    members = _static_members(wheel)

    assert not any("/dashboard/static/" in m for m in members), (
        "with no bundle the wheel must carry NO static/ dir, so _resolve_static_dir falls"
        f" back to the placeholder: {members}"
    )
    with zipfile.ZipFile(wheel) as archive:
        sentinel = [m for m in members if m.endswith(".triage-placeholder")]
        assert sentinel, (
            "the placeholder sentinel must ship, so the runtime warning can fire"
        )
        index = next(
            m for m in members if m.endswith("/dashboard/_placeholder/index.html")
        )
        assert b"placeholder" in archive.read(index)
