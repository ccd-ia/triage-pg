---
title: Emacs, buffer by buffer
description: triage.el — a status buffer, Experiment views and menus for every CLI action, drawn from the CLI's JSON.
sidebar:
  order: 8
  label: Emacs
---

`triage.el` shows a triage-pg Project inside Emacs. `M-x triage-status` opens
a buffer with the Project's Experiments, each matched to its config file, plus
the recent Runs, the declared Sources and the tables of the `triage` schema.
`RET` on an Experiment shows the plan its config implies and what the database
holds for it. `e` opens the YAML. Menus start every action the
[CLI](/triage-pg/reference/cli/) offers.

Emacs never connects to PostgreSQL and never reads a config. It starts the
`triage` CLI in the background and draws the JSON the CLI prints. Every number
on screen therefore comes out of a CLI verb, and you can read the same number
from a terminal.

## Requirements

- Emacs 29.1 or newer.
- The packages `magit-section` (4.0 or newer) and `transient` (0.7 or newer).
  Emacs 29 ships an older `transient`, so install the current one from GNU ELPA.
- triage-pg 1.6.0 or newer in the Project, because the buffers read
  `triage status --json --full` and `triage experiment show --json`.
- [envrc.el](https://github.com/purcell/envrc) if your database credentials
  come from direnv. It sets `PG*` per buffer, and `triage.el` passes that
  environment on to every process it starts.

## Install

The package lives in the `emacs/` directory of the triage-pg repository.

With straight.el:

```elisp
(straight-use-package
 '(triage :host github :repo "ccd-ia/triage-pg" :files ("emacs/*.el")))
```

With the built-in `package-vc` (Emacs 29 or newer):

```elisp
(package-vc-install
 '(triage :url "https://github.com/ccd-ia/triage-pg" :lisp-dir "emacs"))
```

With `use-package` on Emacs 30:

```elisp
(use-package triage
  :vc (:url "https://github.com/ccd-ia/triage-pg" :lisp-dir "emacs")
  :bind ("C-c t" . triage-status))
```

The package binds no global key. The examples use `C-c t`.

## Open a Project

Run `M-x triage-status` (or `M-x triage`) from any buffer inside a Project. The
buffer is called `*triage: PROJECT*`, and calling the command again reuses it.

The Project root is the nearest directory above the current one that has
either:

- a `pyproject.toml` that names the triage-pg repository, such as the
  dependency `"triage @ git+https://github.com/ccd-ia/triage-pg.git@v1.6.0"` or
  the uv source `triage = { path = "../triage-pg" }` (comment lines do not
  count), or
- a `database.yaml`.

Add markers with the option `triage-project-markers`. Every process runs with
the root as its working directory, so `database.yaml` resolves the way it does
in a shell.

The `triage` executable is found in this order, and the status buffer shows
which one it uses:

1. `$UV_PROJECT_ENVIRONMENT/bin/triage`
2. `ROOT/.venv/bin/triage`
3. `triage` on the `exec-path`
4. `uv run triage`

`uv run` comes last because it can re-sync the environment, which adds start-up
time. Set `triage-executable` to a path or a command list to choose it
yourself. With a prefix argument, `C-u M-x triage-status` forgets the cached
roots and executables.

## The status buffer

```
Project:  food        DB: connected · pg 16 · 0028_subset_threshold_curve · 1418 MB
triage:   .venv/bin/triage (1.6.0)

Pending (1)
Experiments (13)
  Slate-Planck-96     classification   6 runs   completed  example/dirtyduck/experiment-guides.yaml
  Pyrite-Pasteur-25   classification   19 runs  completed  4 config files
  fc565f0dd83d        classification   —        not run    example/chicago311/experiment.yaml
Recent runs (5)
Sources (2)
Tables (43)
```

One refresh is one call to `triage status --json --full`. The CLI takes about
a second to start, so the call runs in the background: the command returns at
once, and the mode line says `refreshing…` until the data arrives.

The Experiments section joins two lists on the `experiment_hash`: the
Experiments the database holds, and the config files the CLI found under the
root (it hashes each one). The join gives four kinds of line:

| Line | Meaning |
|---|---|
| a path | the Experiment has exactly one config file |
| `N config files` | several files hash to the same Experiment; they are listed under it |
| `not run` | a config file whose Experiment is not in the database yet |
| `(no config file)` | an Experiment in the database whose config is not under this root |

The buffer lists a config that does not parse with its error. Archived Experiments
are hidden; `A` shows them. When the database cannot be reached, the header
shows the connection error, and the Experiments section still lists the config
files.

| Key | Where | What it does |
|---|---|---|
| `g` | anywhere | refresh |
| `RET` | Experiment, config, Run, table, source | open it |
| `e` | Experiment, config | visit the YAML (asks when there are several files) |
| `TAB`, `M-1`…`M-4` | sections | fold and unfold, as in Magit |
| `A` | anywhere | show or hide archived Experiments |
| `?` or `h` | anywhere | the menu of menus |
| `r` `c` `a` `l` `f` `p` `m` `s` | anywhere | the run, check, analyze, results, score, plot, maintain and serve menus |
| `:` | anywhere | a read-only SQL query, shown as a table |
| `!` | anywhere | any action from `triage actions list` |
| `T` | anywhere | the [terminal cockpit](/triage-pg/reference/tui/) in vterm, or in `ansi-term` |
| `q` | anywhere | bury the buffer |

## The Experiment view

`RET` on an Experiment reads `triage experiment show TARGET --json` once.
TARGET is the config file when there is one, and the hash otherwise. The view
has four sections:

- **Identity**: hash, name, problem type, task framing, and the source (a file
  or the stored config).
- **Validation**: errors and warnings. Folded when there are none.
- **Plan**: what the config implies, computed without the database. Splits,
  as_of_dates, label timespans, matrices to build, grid size, model groups,
  feature-group runs, and the number of models to train, in bold. Then the
  subsets and any baseline issues.
- **History**: the Experiment's Runs, its model groups (average ± standard
  deviation and maximum regret on the experiment's most-evaluated metric) and
  the top ten rows of its leaderboard. It says `not run yet` when the database
  has no row for the hash.

`e` visits the config file. For an Experiment opened by its hash, `e` shows the
stored config as YAML, which the CLI formats, in a read-only buffer. `RET` on a
Run opens the Run view, on a model group its member models, and on a
leaderboard row the model.

## The other views

| View | Opened by | CLI call |
|---|---|---|
| Run | `RET` on a Run | `triage runs show ID --json`; `t` follows it with `triage runs tail ID` |
| Model group | `RET` on a model group | `triage models HASH --group G --json` |
| Model | `RET` on a model | `triage model show ID --json` |
| Table | `RET` on a table | `triage query --json "select * from triage.NAME limit 200"` |
| Query | `:` | `triage query --json SQL` |

The table browser builds the only SQL text in `triage.el`. The relation name
comes from the CLI's own listing, `triage.el` quotes it, and the CLI runs every
query in a read-only transaction. `triage-table-limit` sets how many rows it reads.

## The menus

Each menu entry builds the arguments of one CLI verb. The Experiment, Run,
model or model group at point fills the target; otherwise the entry asks for
one. The menu greys out an entry that needs a target when there is none at point
and none to choose from.

| Menu | CLI verb | Output |
|---|---|---|
| run `r` | `triage run CONFIG` with `--profile`, `--random-seed`, `--cache-policy`, `--project-path` | a process buffer; the status buffer refreshes when the run ends. With `--profile=cloud` the CLI only submits the job to AWS Batch. |
| check `c` | `triage check leakage CONFIG --json FILE` with `--dates` | exit 0: a message that nothing differs; exit 1: the report; any other exit: the error |
| analyze `a` | `triage analyze-config CONFIG` with `--estimate`, `--estimate-dates`, `--features`, `--plot` | a process buffer; with `--plot`, the splits figure opens when it ends |
| results `l` | `triage leaderboard`, `triage leaderboard --refresh`, `triage audition`, `triage models` with `--metric`, `--parameter`, `--windowed` | table views |
| score `f` | `triage score MODEL [DATE]` with `--cohort-query`, `--project-path` | a process buffer |
| plot `p` | `triage plot curve`, `error-rules`, `crosstabs` (a model) and `curve-dates`, `leaves`, `recurrence`, `crosstabs-dates`, `overlap` (a model group) | the figure, in an image buffer |
| maintain `m` | `triage archive HASH`, `triage gc`, `triage gc --delete`, `triage db upgrade` | a process buffer; archive and delete ask first |
| serve `s` | `triage serve` with `--port` | a process buffer; the browser opens when the dashboard listens |

The plot menu passes each subcommand only the flags it accepts, and asks for
the cut when `leaves`, `recurrence` or `overlap` needs one. It writes each
figure to a temporary file and deletes the file with its buffer.

`triage reset` is in no menu. It deletes results, so it is reachable only
through `!`, where the CLI's own confirmation applies.

## Errors

A failed call puts its last line in the echo area (or in the header of the
buffer that made it) and appends the command, its exit code and its full error
output to the `*triage-errors*` buffer.

The CLI marks its JSON with a contract number. If triage-pg is newer than
`triage.el`, the status buffer says `triage-pg is newer than triage.el; update
triage.el`. If the CLI is too old to print the status JSON, it says which
triage-pg version is needed.
