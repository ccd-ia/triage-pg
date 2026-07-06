"""Reproducible dashboard screenshot pass (v1-release plan P8).

Captures the docs/README screenshot set against a LIVE stack (real tutorial data, not
fixtures) so the set can be regenerated after any UI change:

    # stack: tutorial DB(s) up + experiments run + dashboard served, e.g.
    #   TRIAGE_REGISTRY_URL=… TRIAGE_PROJECT_DB_MAP=… just serve 8014
    uv run python scripts/capture_screenshots.py --base http://127.0.0.1:8014
    uv run python scripts/capture_screenshots.py --check   # files exist + sane

Playwright (dev extra) drives headless Chromium at 1440x900, light theme. Every shot
asserts a data-bearing element before shooting — a blank panel fails loud rather than
committing an empty screenshot.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "images"
VIEWPORT = {"width": 1440, "height": 900}

# plain routes: (filename, path, wait-for selector)
ROUTE_SHOTS = [
    ("experiments-list.png", "/experiments", "table"),
    ("experiment-overview.png", "/experiments/{exp}", ".grid .cell"),
    ("monitoring-view.png", "/monitoring", ".panel, main"),
    ("submissions-form.png", "/submissions", "form"),
    ("projects-view.png", "/projects", "table, .panel"),
]

# experiment sub-tabs are React state (not URL params) — click the TabBtn by label.
TAB_SHOTS = [
    ("model-groups-table.png", "Model Groups", ".panel table"),
    ("audition-tab.png", "Audition", ".panel"),
    ("bias-tab.png", "Bias", ".fairgrid"),
]

SHOT_NAMES = (
    [f for f, *_ in ROUTE_SHOTS] + [f for f, *_ in TAB_SHOTS] + ["model-sheet.png"]
)


def capture(base: str) -> int:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, color_scheme="light")

        # resolve the first experiment hash from the API (shots are {exp}-templated)
        resp = page.request.get(f"{base}/api/experiments")
        experiments = resp.json()
        if not experiments:
            print("no experiments served — run one first", file=sys.stderr)
            return 1
        exp = experiments[0]["experiment_hash"]

        def shoot(filename: str, selector: str) -> None:
            page.wait_for_selector(selector, timeout=15000)
            # charts animate; SSE keeps the network busy (ADR-0021), so the
            # selector-wait above is the readiness signal, not networkidle.
            page.wait_for_timeout(1200)
            page.screenshot(path=str(OUT / filename), full_page=False)
            print(f"captured {filename}")

        for filename, path, selector in ROUTE_SHOTS:
            try:
                page.goto(base + path.format(exp=exp), wait_until="domcontentloaded")
                shoot(filename, selector)
            except (
                Exception
            ) as exc:  # noqa: BLE001 — report every failed shot, then fail
                failures.append(f"{filename}: {exc}")
                print(f"FAILED {filename}: {exc}", file=sys.stderr)

        try:
            page.goto(f"{base}/experiments/{exp}", wait_until="domcontentloaded")
            page.wait_for_selector(".subtabs", timeout=30000)
        except Exception as exc:  # noqa: BLE001 — tab shots all depend on this page
            failures.extend(f"{f}: detail page failed ({exc})" for f, *_ in TAB_SHOTS)
            print(f"FAILED experiment detail page: {exc}", file=sys.stderr)
        for filename, tab_label, selector in TAB_SHOTS:
            try:
                page.locator(".subtabs button", has_text=tab_label).first.click()
                shoot(filename, selector)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{filename}: {exc}")
                print(f"FAILED {filename}: {exc}", file=sys.stderr)

        # the model sheet is a side-sheet opened from an overview heatmap cell
        try:
            page.locator(".subtabs button", has_text="Overview").first.click()
            page.wait_for_selector(".grid .cell", timeout=15000)
            page.locator(".grid .cell:not(.empty)").first.click()
            page.wait_for_selector("aside.sheet", timeout=15000)
            page.wait_for_timeout(1200)
            page.screenshot(path=str(OUT / "model-sheet.png"))
            print("captured model-sheet.png")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"model-sheet.png: {exc}")
            print(f"FAILED model-sheet.png: {exc}", file=sys.stderr)

        browser.close()
    if failures:
        print(f"\n{len(failures)} shot(s) failed", file=sys.stderr)
        return 1
    return 0


def check() -> int:
    missing = []
    for filename in SHOT_NAMES:
        f = OUT / filename
        if not f.exists() or f.stat().st_size < 10_000:
            missing.append(filename)
    if missing:
        print(f"missing/too-small: {missing}", file=sys.stderr)
        return 1
    print(f"all {len(SHOT_NAMES)} screenshots present under {OUT}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8014")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    sys.exit(check() if args.check else capture(args.base))
