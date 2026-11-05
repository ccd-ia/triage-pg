# 0035. Plots are a CLI surface over one query layer shared with the dashboard

- Status: Accepted
- Date: 2026-11-05
- Deciders: Adolfo De Unánue

## Context

ADR-0011 dissolved postmodeling into persisted diagnostics, SQL views and dashboard
panels, so every picture of a model lived in a panel. The v1.4.0 reading guides then
taught users to read the threshold curve, the error trees and the crosstabs with 25
figures drawn from DirtyDuck by `scripts/docs_figures.py`. That script is not installed
with triage-pg, is tied to the guides' Experiment, and read through
`triage.dashboard.routes`, which imports FastAPI at module level. A user could reproduce
the guides' numbers on their own data but not their figures, and a report, notebook or
slide needs a file, which a panel does not give.

The SQL behind each panel lived inside its FastAPI handler. A plot module that queried
the database directly would have copied the handlers' filters and orderings (the
crosstabs ranking, the error-rule branches, Subset resolution), and the two copies could
drift until a figure and a panel disagreed on the same model.

## Decision

- **One query layer.** The SQL the dashboard's curve, crosstabs, error-rule, recurrence,
  list-overlap and group routes ran moves into `triage.diagnostics.readers`: plain
  functions over a connection pool, with no FastAPI import. The routes became thin
  wrappers that keep their URLs, parameters and response shapes, and the existing route
  tests passed unchanged through the move. Each reader is still a query over the existing
  SQL functions, so no migration was needed.
- **Plots are files, from the CLI.** `triage.plots` draws eight figures from the readers
  and `triage plot <kind>` writes them as `.svg`, `.png` or `.pdf`, chosen by the
  extension. It needs no dashboard extra. Each plot function returns the rows it drew,
  so a test checks a figure against the readers, and an SVG is byte-identical across
  runs. Exit codes: 0 written, 1 nothing to draw (the message names the command that
  creates the rows), 2 bad input or a database or file error.
- **The dashboard keeps the views used while browsing.** Four additions read the same
  readers: nonzero rates in the crosstabs panel, the population error rate as a mark on
  the error-rule bars, several Subsets on the model's curve, and the curve across a model
  group's test dates. The panels draw only fields the routes return.

### Alternatives considered

- **Plots only in the dashboard.** Rejected: a panel gives no file for a report, and the
  comparisons the guides draw (two models, six dates, four Subsets) would each need a
  panel built for a single figure.
- **Plots behind the dashboard extra, reading the routes.** Rejected: a user who only
  wants a PDF would install FastAPI and the web stack.
- **Plotting in SQL, or a plot module with its own SQL.** Rejected: SQL cannot draw, and
  a second copy of each query is exactly the drift this decision removes.

## Consequences

- The command names and their flags are public. `triage plot` holds eight commands:
  `curve`, `curve-dates`, `error-rules`, `leaves`, `recurrence`, `crosstabs`,
  `crosstabs-dates`, `overlap`.
- A new diagnostic that should be both drawn and browsed adds a reader first, then a
  route and a plot function over it.
- 14 of the guides' 19 worked figures are now drawn by `triage.plots`, and each guide
  gives the `triage plot` command that draws the figure on the reader's own data. The
  other five (calibration, score histogram, Subset metrics, feature ablation, bias) stay
  in the docs script, because the dashboard already draws them per model.
- A drawn tree diagram, interactive or HTML output, and CLI plots for the five figures
  above are out of scope.
