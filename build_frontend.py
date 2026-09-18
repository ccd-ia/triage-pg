"""Hatchling build hook: put the built dashboard SPA inside the wheel (#15).

`packages = ["src/triage"]` shipped `triage/dashboard/static/` with only the 620-byte
placeholder in it, so an install from git or a wheel served HTTP 200 and a blank-looking page for
every route. `_SpaStaticFiles` falls back to `index.html` for any non-`/api` path, so the user
saw a 200 and reasonably concluded the dashboard was broken or their data was missing. Nothing in
the README or the quickstart said a package install ships no bundle; `just serve` silently relies
on `frontend/dist` existing in a source checkout.

**Reuse before rebuilding.** CI's `frontend` job and the Dockerfile's `frontend-build` stage both
already run `npm ci && npm run build`, so in both places `frontend/dist` exists before the wheel
is built. Re-running npm there would be pure latency, and worse, it would make the wheel's
contents depend on a second npm resolution rather than on the artifact CI just verified.

**Never fail the build.** With no `frontend/dist` and no `npm` on PATH the hook warns loudly and
leaves the placeholder, so the wheel still installs on a machine without a node toolchain — the
dashboard is then exactly as absent as it is today, rather than the install being impossible. The
runtime placeholder and the startup warning name the two commands that fix it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

#: Where the SPA is served from inside the installed package.
PACKAGED_STATIC = "triage/dashboard/static"

#: Set to "1" to skip the npm build entirely (CI builds the bundle in its own job).
SKIP_ENV = "TRIAGE_SKIP_FRONTEND_BUILD"


# basedpyright wants type arguments here, but hatchling's own generics recurse
# (BuildHookInterface[BuilderConfig, PluginManager] and BuilderConfig is itself generic),
# so parameterising buys nothing for a build-time plugin class.
class FrontendBuildHook(BuildHookInterface):  # pyright: ignore[reportMissingTypeArgument]
    """Build (or reuse) `frontend/dist` and force-include it into the wheel."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        dist = root / "frontend" / "dist"

        if not (dist / "index.html").exists():
            self._build(root, dist)

        if not (dist / "index.html").exists():
            self.app.display_warning(
                "triage: no dashboard SPA in this wheel. `triage.dashboard` will serve the"
                " placeholder page, exactly as it does today. To ship the bundle, run"
                " `cd frontend && npm ci && npm run build` before building, or install node"
                " and rebuild. At runtime you can also point TRIAGE_DASHBOARD_STATIC at a"
                " directory holding a built SPA."
            )
            return

        # Map every built file to its path inside the package. force_include takes
        # {source: relative-destination}; a directory maps wholesale.
        build_data.setdefault("force_include", {})[str(dist)] = PACKAGED_STATIC
        self.app.display_info(f"triage: including the dashboard SPA from {dist}")

    def _build(self, root: Path, dist: Path) -> None:
        """Run the Vite build, unless it is skipped or npm is unavailable."""
        if os.environ.get(SKIP_ENV) == "1":
            self.app.display_info(
                f"triage: {SKIP_ENV}=1 — not building the dashboard SPA"
            )
            return

        frontend = root / "frontend"
        if not (frontend / "package.json").exists():
            return  # an sdist without frontend/ — nothing to build, nothing to say

        npm = shutil.which("npm")
        if npm is None:
            self.app.display_warning(
                "triage: npm is not on PATH, so the dashboard SPA cannot be built here."
            )
            return

        self.app.display_info(
            "triage: building the dashboard SPA (npm ci && npm run build)"
        )
        for command in (["ci"], ["run", "build"]):
            result = subprocess.run(  # noqa: S603 — npm off PATH, no shell
                [npm, *command],
                cwd=frontend,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                # Loud, with the reason, and then fall through to the placeholder. A failed
                # SPA build must not make `uv pip install git+…` impossible.
                self.app.display_warning(
                    f"triage: `npm {' '.join(command)}` failed in {frontend}"
                    f" (exit {result.returncode}). The wheel will ship the placeholder."
                    f"\n{(result.stderr or result.stdout).strip()[:2000]}"
                )
                return
        if not (dist / "index.html").exists():
            self.app.display_warning(
                f"triage: npm run build completed but {dist}/index.html is missing."
            )
