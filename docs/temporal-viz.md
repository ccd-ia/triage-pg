# Temporal cross-validation viz

triage-pg renders a `temporal_config`'s **temporal cross-validation blocks** — the train/
validation matrices Timechop produces across time, their as-of dates, and each matrix's label
window — so the point-in-time structure of an experiment is visible at a glance. The same design
is available two ways: the CLI (a static image or a self-contained interactive HTML) and a live
panel in the read dashboard.

## How to read it

- **One lane per split, most recent on top.** Each split is one train → validate cut of time.
- **Train (blue) and validation (orange)** are fixed, distinct colors. The solid bar is the
  matrix's **as-of-date span**; the small ringed markers are the individual **as-of dates** (they
  stay visible even when a validation matrix is a single date — a zero-width span).
- **The lighter extension past each bar is the label window** — the outcome lookahead
  (`as-of date + label timespan`). Validation's label window sits entirely *after* train's, which
  is the point-in-time separation: no label from validation leaks back into training.
- **The dashed green line is the feature-availability start** for that split.

## CLI — `triage analyze-config --plot`

`analyze-config` already prints the split/grid counts; `--plot` also renders the blocks. The
format is inferred from the extension — `.png`/`.svg`/`.pdf` give a static image (matplotlib),
`.html` an interactive, self-contained page (plotly, also embeddable elsewhere):

```bash
uv run triage analyze-config example/dirtyduck/experiment.yaml --plot blocks.png
uv run triage analyze-config example/dirtyduck/experiment.yaml --plot blocks.html   # interactive
```

![Temporal cross-validation blocks rendered by `triage analyze-config --plot` for the DirtyDuck
example: four splits, train (blue) and validation (orange) matrices with their as-of dates and
label-window lookaheads, the feature-start line, and per-split label timespans.](images/temporal-blocks-cli.png)

## Dashboard — the *Temporal configuration* card

On an experiment's detail screen, the **Config** tab's *Temporal configuration* card renders the
same blocks live from the stored config (`POST /api/temporal-viz` computes the Timechop splits;
the SPA draws them as a theme-aware SVG). No plotting runs server-side — the endpoint returns
plain ISO dates and nothing is persisted.

![The dashboard Temporal configuration panel: the temporal cross-validation blocks for an
experiment, with the train/validation/label-window legend, two splits (most recent on top), the
feature-start line, the year axis, and per-split label timespans.](images/temporal-blocks-dashboard.png)

## See also

- [`adapter-spec.md`](adapter-spec.md) — the `temporal_config` → Timechop seam.
- The `timechop` component (`src/triage/component/timechop/`) — the split generator; the viz lives
  in its `plotting.py`.
