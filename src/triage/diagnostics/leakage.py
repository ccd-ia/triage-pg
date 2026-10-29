"""The leakage check: as-of invariance by delete-and-rollback (ADR-0034).

triage-pg controls the temporal splits, featurizer's causal predicate and the train-only
imputation fit. It cannot inspect the three things the user writes: the cohort query, the
label query, and the knowledge dates their Sources declare. This module tests those three
for one property, exactly and with no statistics:

* the cohort at ``t`` must not change when every row known **after** ``t`` is deleted;
* the features at ``t`` must not change when every event row known **on or after** ``t``
  is deleted (entity rows: known after ``t``, since an entity in the cohort at ``t`` is
  known at ``t``);
* the labels at ``(t, lt)`` must not change when every row known **on or after**
  ``t + lt`` is deleted, which is the end of the label window ``[t, t + lt)``.

``t`` runs over every distinct ``as_of_date`` of the temporal config, and ``lt`` over the
label timespans each date is used with. A row's knowledge date is the
``knowledge_date_column`` its Source declares.

The harness
-----------
:func:`check_leakage` builds the cohort, labels and features once on all data (the
baseline), then cuts the Sources and rebuilds each artifact after the cut that must not
affect it. All of it runs on **one connection, inside one transaction that always ends in
ROLLBACK**:

1. The cuts are sorted in descending order, so the deletes are cumulative and each row is
   deleted at most once.
2. Other sessions never see the deletes (MVCC). A deferred constraint on a temporary table
   makes any ``COMMIT`` on the connection fail, so even a library that committed by mistake
   could not make the deletes permanent. This module has no commit call; a test enforces it.
3. featurizer reads on the same connection. ``Featurizer.to_arrow(connection=conn)`` runs its
   statements under savepoints and commits only connections it opened itself (pinned
   featurizer 1.3.1; a tripwire test in ``test_leakage_check.py`` guards the bump). Each
   feature build runs inside a savepoint that is rolled back, which drops its temp tables.
4. Afterwards every cut Source's fingerprint (row count + max knowledge date) is read again
   on a fresh connection and compared with the one taken before the run.

A difference is a fact, not a p-value: it names the component, the date and the entities,
and for features the columns, whose names encode the relationship and aggregation path that
read the data.

Relations the queries read but the config does not declare as Sources are found through
``explain (format json)`` (no SQL parsing, ADR-0014) and reported as not checked.
"""

from __future__ import annotations

import math
import tempfile
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, LiteralString, cast

import psycopg
from psycopg import pq
from pydantic import ValidationError

from triage.adapters.cohort import render_cohort_query
from triage.adapters.labels import label_value_columns, render_label_query
from triage.adapters.matrix import (
    _featurizer_config_yaml,
    _materialize_pairs,
    _merge_arrow_groups,
    _missing_suffix,
)
from triage.adapters.problem_config import cohort_config_errors, label_config_errors
from triage.adapters.run import _as_dates, _featurizer_only, _generate_splits
from triage.adapters.temporal import TemporalConfig
from triage.logging import get_logger
from triage.sources import _quote_column, _quote_relation, fingerprint_relation
from triage.util.conf import convert_str_to_relativedelta
from triage.util.db import libpq_conninfo

if TYPE_CHECKING:
    import polars as pl

logger = get_logger(__name__)

__all__ = [
    "Check",
    "CutKey",
    "CutPlan",
    "Finding",
    "LeakageCheckError",
    "LeakageReport",
    "NotChecked",
    "SourceSpec",
    "check_leakage",
    "compare_cohort",
    "compare_features",
    "compare_labels",
    "cuttable_sources",
    "declared_sources",
    "delete_after",
    "discover_relations",
    "fingerprints",
    "plan_cuts",
    "preflight_privileges",
]

Component = Literal["cohort", "labels", "features"]

#: Feature values are compared with this tolerance: aggregation order can move the last bits.
RTOL = 1e-9
ATOL = 1e-12

#: How many example entity ids a finding carries.
MAX_EXAMPLES = 10

#: A concurrent writer holding a row lock makes the check fail fast instead of waiting.
LOCK_TIMEOUT = "5s"

_DELETE_SAVEPOINT = "triage_leakage_delete"
_FEATURES_SAVEPOINT = "triage_leakage_features"

# A deferred foreign key from one temp table to an empty one: every statement passes, and
# the check at COMMIT fails, so the transaction cannot commit (see _arm_commit_guard).
_COMMIT_GUARD = (
    "create temp table triage_leakage_guard_parent (id integer primary key)",
    "create temp table triage_leakage_guard (id integer"
    + " references triage_leakage_guard_parent deferrable initially deferred)",
    "insert into triage_leakage_guard (id) values (1)",
)

# Schemas whose relations are never reported as undeclared reads.
_SYSTEM_SCHEMAS = ("triage", "pg_catalog", "information_schema")


class LeakageCheckError(RuntimeError):
    """The check could not run. The message says what failed, on what, and what to try."""


def _sql(text: str) -> LiteralString:
    # cast: the check runs operator-supplied template SQL and validated, quoted
    # identifiers — dynamic by design, so psycopg's LiteralString guard cannot apply.
    return cast(LiteralString, text)


# --------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    """One Source as the experiment config declares it (``sources:``)."""

    name: str
    relation: str
    knowledge_date_column: str | None = None
    role: str | None = None

    @property
    def is_entity(self) -> bool:
        """Entity Sources are cut after event Sources and keep rows known at the cut."""
        return self.role == "entity"


def declared_sources(config: Mapping[str, Any]) -> list[SourceSpec]:
    """The Sources the experiment config declares, validated.

    Raises:
        LeakageCheckError: when a Source has no name or relation, or names a relation or
            column that is not a plain (schema-qualified) identifier.
    """
    specs: list[SourceSpec] = []
    for i, raw in enumerate(config.get("sources") or []):
        if (
            not isinstance(raw, Mapping)
            or not raw.get("name")
            or not raw.get("relation")
        ):
            raise LeakageCheckError(
                f"sources[{i}] needs a 'name' and a 'relation'; got {raw!r}"
            )
        kdc, role = raw.get("knowledge_date_column"), raw.get("role")
        spec = SourceSpec(
            name=str(raw["name"]),
            relation=str(raw["relation"]),
            knowledge_date_column=str(kdc) if kdc else None,
            role=str(role) if role else None,
        )
        try:
            _quote_relation(spec.relation)
            if spec.knowledge_date_column is not None:
                _quote_column(spec.knowledge_date_column)
        except ValueError as exc:
            raise LeakageCheckError(f"Source {spec.name!r}: {exc}") from exc
        specs.append(spec)
    return specs


def cuttable_sources(
    sources: Sequence[SourceSpec],
) -> tuple[list[SourceSpec], list[SourceSpec]]:
    """Split the declared Sources into (cuttable, uncut).

    A Source is cuttable when it declares a ``knowledge_date_column``. The others are read
    uncut, and the report lists them as not checked.
    """
    cuttable = [s for s in sources if s.knowledge_date_column]
    uncut = [s for s in sources if not s.knowledge_date_column]
    return cuttable, uncut


def fingerprints(
    conninfo: str, sources: Sequence[SourceSpec]
) -> dict[str, dict[str, Any]]:
    """Fingerprint every Source on a fresh connection (row count + max knowledge date)."""
    try:
        conn = psycopg.connect(libpq_conninfo(conninfo), autocommit=True)
    except psycopg.Error as exc:
        raise LeakageCheckError(
            f"could not connect to the project database to fingerprint the Sources: {exc}"
        ) from exc
    try:
        out: dict[str, dict[str, Any]] = {}
        for source in sources:
            try:
                out[source.name] = fingerprint_relation(
                    conn, source.relation, source.knowledge_date_column
                )
            except psycopg.Error as exc:
                raise LeakageCheckError(
                    f"could not read Source {source.name!r} ({source.relation}): "
                    + f"{exc}. Check its relation and knowledge_date_column in sources:."
                ) from exc
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------------------
# The cut plan
# --------------------------------------------------------------------------------------


def _as_datetime(value: date) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day)


def _iso(value: datetime) -> str:
    if value.hour == value.minute == value.second == value.microsecond == 0:
        return value.date().isoformat()
    return value.isoformat()


@dataclass(frozen=True, order=True)
class CutKey:
    """One cut: delete every row whose knowledge date is not kept at ``at``.

    A row known exactly at ``at`` is kept in an entity Source when ``keep_equal_entity`` is
    true, and in any other Source when ``keep_equal_event`` is true. Cut keys sort by
    ``(at, keep_equal_entity, keep_equal_event)``; processed in descending order, each cut
    deletes a superset of what the previous one deleted.
    """

    at: datetime
    keep_equal_entity: bool
    keep_equal_event: bool

    def keeps_equal(self, source: SourceSpec) -> bool:
        """Whether a row of ``source`` known exactly at ``at`` survives this cut."""
        return self.keep_equal_entity if source.is_entity else self.keep_equal_event

    def describe(self) -> str:
        """The rows this cut deletes, in words."""
        at = _iso(self.at)
        if self.keep_equal_entity and self.keep_equal_event:
            return f"rows known after {at}"
        if not self.keep_equal_entity and not self.keep_equal_event:
            return f"rows known on or after {at}"
        event = "after" if self.keep_equal_event else "on or after"
        entity = "after" if self.keep_equal_entity else "on or after"
        return f"event rows known {event} {at}, entity rows known {entity} {at}"


@dataclass(frozen=True)
class Check:
    """One artifact at one date, and the cut that must leave it unchanged."""

    component: Component
    as_of_date: date
    label_timespan: str | None = None

    @property
    def cut(self) -> CutKey:
        """Cohort C(t): keep kd <= t. Features F(t): keep kd < t for event Sources and
        kd <= t for entity Sources. Labels L(t, lt): keep kd < t + lt."""
        start = _as_datetime(self.as_of_date)
        if self.component == "cohort":
            return CutKey(start, True, True)
        if self.component == "features":
            return CutKey(start, True, False)
        if self.label_timespan is None:
            raise ValueError("a labels check needs its label_timespan")
        end = start + convert_str_to_relativedelta(self.label_timespan)
        return CutKey(_as_datetime(end), False, False)


@dataclass(frozen=True)
class CutPlan:
    """Every check of the run and the cuts, in the order they are applied (descending)."""

    as_of_dates: tuple[date, ...]
    label_timespans: Mapping[date, tuple[str, ...]]
    checks: tuple[Check, ...]
    cuts: tuple[CutKey, ...]

    def checks_at(self, cut: CutKey) -> list[Check]:
        """The checks whose artifact is rebuilt right after ``cut``."""
        return [check for check in self.checks if check.cut == cut]


def _assert_cumulative(cuts: Sequence[CutKey]) -> None:
    """Each cut must delete a superset of the previous one, for both kinds of Source."""
    for earlier, later in zip(cuts, cuts[1:]):
        if later.at > earlier.at or (
            later.at == earlier.at
            and (
                later.keep_equal_entity > earlier.keep_equal_entity
                or later.keep_equal_event > earlier.keep_equal_event
            )
        ):
            raise ValueError(
                f"cut {later} would keep rows that cut {earlier} already deleted;"
                + " the cuts must be cumulative"
            )


def plan_cuts(
    config: Mapping[str, Any], dates: Sequence[date] | None = None
) -> CutPlan:
    """Plan the checks and the cuts from the config's temporal splits (no database).

    Each distinct ``as_of_date`` gets a cohort and a features check, plus one labels check
    per label timespan it is used with: ``training_label_timespan`` on train dates,
    ``test_label_timespan`` on test dates. A date can carry both.

    Args:
        config: the experiment config; only ``temporal_config`` is read.
        dates: restrict the plan to these as_of_dates. Default: every date.

    Raises:
        LeakageCheckError: on an invalid temporal_config, or a requested date that is not
            an as_of_date of it.
    """
    raw = config.get("temporal_config")
    if not isinstance(raw, Mapping):
        raise LeakageCheckError("the experiment config needs a temporal_config mapping")
    try:
        temporal = TemporalConfig.model_validate(dict(raw))
        splits = _generate_splits(temporal)
    except (ValidationError, ValueError) as exc:
        raise LeakageCheckError(f"temporal_config is not valid: {exc}") from exc

    timespans: dict[date, list[str]] = defaultdict(list)

    def _add(as_of_times: Iterable[Any], timespan: str) -> None:
        for as_of_date in _as_dates(list(as_of_times)):
            if timespan not in timespans[as_of_date]:
                timespans[as_of_date].append(timespan)

    for split in splits:
        train = split["train_matrix"]
        _add(train["as_of_times"], train["training_label_timespan"])
        for test in split["test_matrices"]:
            _add(test["as_of_times"], test["test_label_timespan"])

    planned = sorted(timespans)
    if dates is not None:
        requested = sorted(set(dates))
        unknown = [d for d in requested if d not in timespans]
        if unknown:
            raise LeakageCheckError(
                f"{', '.join(d.isoformat() for d in unknown)}: not an as_of_date of this"
                + " temporal_config. Its as_of_dates are "
                + ", ".join(d.isoformat() for d in planned)
                + " (see `triage analyze-config CONFIG`)."
            )
        planned = requested

    checks: list[Check] = []
    for as_of_date in planned:
        checks.append(Check("cohort", as_of_date))
        checks.append(Check("features", as_of_date))
        checks.extend(Check("labels", as_of_date, lt) for lt in timespans[as_of_date])
    cuts = tuple(sorted({check.cut for check in checks}, reverse=True))
    _assert_cumulative(cuts)
    return CutPlan(
        as_of_dates=tuple(planned),
        label_timespans={d: tuple(timespans[d]) for d in planned},
        checks=tuple(checks),
        cuts=cuts,
    )


# --------------------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


@dataclass(frozen=True)
class Finding:
    """One artifact that changed after the cut that must not affect it.

    ``added``/``removed`` count entities (cohort), label rows or feature rows that appear or
    disappear; ``changed`` counts label rows or feature rows whose value changed. For
    features, ``columns`` holds ``(column, rows changed)`` pairs, most-changed first.
    """

    component: Component
    as_of_date: date
    cut: str
    label_timespan: str | None = None
    added: int = 0
    removed: int = 0
    changed: int = 0
    examples: tuple[Any, ...] = ()
    columns: tuple[tuple[str, int], ...] = ()

    @property
    def n_entities(self) -> int:
        """Entities (cohort) or rows (labels, features) that differ."""
        return self.added + self.removed + self.changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "as_of_date": self.as_of_date.isoformat(),
            "label_timespan": self.label_timespan,
            "cut": self.cut,
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
            "n_entities": self.n_entities,
            "examples": [_json_safe(v) for v in self.examples],
            "columns": {name: n for name, n in self.columns},
        }


@dataclass(frozen=True)
class NotChecked:
    """A relation the check read but did not cut, so leakage through it is not tested."""

    relation: str
    reason: str
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"relation": self.relation, "source": self.source, "reason": self.reason}


@dataclass
class LeakageReport:
    """The result of :func:`check_leakage`. An empty ``findings`` list means no leakage."""

    findings: list[Finding] = field(default_factory=list)
    not_checked: list[NotChecked] = field(default_factory=list)
    dates_checked: list[date] = field(default_factory=list)
    label_checks: list[tuple[date, str]] = field(default_factory=list)
    sources_cut: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def leakage_found(self) -> bool:
        return bool(self.findings)

    def add(self, findings: Iterable[Finding]) -> None:
        self.findings.extend(findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "leakage_found": self.leakage_found,
            "dates_checked": [d.isoformat() for d in self.dates_checked],
            "label_checks": [
                {"as_of_date": d.isoformat(), "label_timespan": lt}
                for d, lt in self.label_checks
            ],
            "sources_cut": list(self.sources_cut),
            "elapsed_s": round(self.elapsed_s, 3),
            "findings": [f.to_dict() for f in self.findings],
            "not_checked": [n.to_dict() for n in self.not_checked],
        }


# --------------------------------------------------------------------------------------
# Comparisons (pure; no database)
# --------------------------------------------------------------------------------------


def _examples(values: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(sorted(values)[:MAX_EXAMPLES])


def compare_cohort(
    check: Check,
    baseline: set[Any] | frozenset[Any],
    observed: set[Any] | frozenset[Any],
) -> list[Finding]:
    """Entities added to or removed from the cohort by the cut."""
    added = set(observed) - set(baseline)
    removed = set(baseline) - set(observed)
    if not added and not removed:
        return []
    return [
        Finding(
            component="cohort",
            as_of_date=check.as_of_date,
            cut=check.cut.describe(),
            added=len(added),
            removed=len(removed),
            examples=_examples(added | removed),
        )
    ]


def _same_value(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    numeric = (int, float, Decimal)
    if (
        isinstance(a, numeric)
        and isinstance(b, numeric)
        and not isinstance(a, bool)
        and not isinstance(b, bool)
    ):
        fa, fb = float(a), float(b)
        if math.isnan(fa) or math.isnan(fb):
            return math.isnan(fa) and math.isnan(fb)
        return math.isclose(fa, fb, rel_tol=RTOL, abs_tol=ATOL)
    return a == b


def _same_rows(a: Sequence[tuple[Any, ...]], b: Sequence[tuple[Any, ...]]) -> bool:
    return len(a) == len(b) and all(
        len(x) == len(y) and all(_same_value(u, v) for u, v in zip(x, y))
        for x, y in zip(a, b)
    )


def compare_labels(
    check: Check,
    baseline: Mapping[Any, Sequence[tuple[Any, ...]]],
    observed: Mapping[Any, Sequence[tuple[Any, ...]]],
) -> list[Finding]:
    """Label rows that appear, disappear or change value after the cut."""
    appeared = observed.keys() - baseline.keys()
    disappeared = baseline.keys() - observed.keys()
    changed = {
        entity
        for entity in baseline.keys() & observed.keys()
        if not _same_rows(baseline[entity], observed[entity])
    }
    if not appeared and not disappeared and not changed:
        return []
    return [
        Finding(
            component="labels",
            as_of_date=check.as_of_date,
            label_timespan=check.label_timespan,
            cut=check.cut.describe(),
            added=len(appeared),
            removed=len(disappeared),
            changed=len(changed),
            examples=_examples(appeared | disappeared | changed),
        )
    ]


def _values_differ(baseline: pl.Series, observed: pl.Series):
    """Row-wise "differs" mask: NaN/NULL equal each other; numbers within RTOL/ATOL."""
    import numpy as np
    import polars as pl

    def _numeric(series: pl.Series) -> bool:
        return series.dtype.is_numeric() or series.dtype == pl.Boolean

    if _numeric(baseline) and _numeric(observed):
        b = baseline.cast(pl.Float64).fill_null(float("nan")).to_numpy()
        o = observed.cast(pl.Float64).fill_null(float("nan")).to_numpy()
        return ~np.isclose(o, b, rtol=RTOL, atol=ATOL, equal_nan=True)
    same = baseline.cast(pl.String).eq_missing(observed.cast(pl.String))
    return ~same.to_numpy()


def compare_features(
    check: Check,
    baseline: pl.DataFrame,
    observed: pl.DataFrame,
    keys: Sequence[str],
) -> list[Finding]:
    """Feature rows and values that differ after the cut, per column.

    Rows are matched on ``keys`` (``as_of_date`` and the target id). ``__missing`` flag
    columns are skipped: a flag only moves when its feature's value does.

    Args:
        check: the features check (its date and cut label the finding).
        baseline: the features on all data.
        observed: the features after the cut, for the same cohort pairs.
        keys: the key columns, the target id last.
    """
    import numpy as np

    id_col = keys[-1]
    suffix = _missing_suffix()
    key_frame_b = baseline.select(keys)
    key_frame_o = observed.select(keys)
    removed = key_frame_b.join(key_frame_o, on=list(keys), how="anti")
    added = key_frame_o.join(key_frame_b, on=list(keys), how="anti")
    joined = baseline.join(observed, on=list(keys), how="inner", suffix="__observed")

    columns = [
        c for c in baseline.columns if c not in keys and not c.endswith(suffix)
    ] + [
        c
        for c in observed.columns
        if c not in keys and not c.endswith(suffix) and c not in baseline.columns
    ]
    any_changed = np.zeros(joined.height, dtype=bool)
    per_column: list[tuple[str, int]] = []
    for column in columns:
        if column not in observed.columns or column not in baseline.columns:
            # The column itself appeared or vanished: every row differs.
            mask = np.ones(joined.height, dtype=bool)
        else:
            mask = _values_differ(joined[column], joined[f"{column}__observed"])
        n = int(mask.sum())
        if n:
            per_column.append((column, n))
            any_changed |= mask

    n_changed = int(any_changed.sum())
    if not n_changed and not removed.height and not added.height:
        return []
    changed_ids = joined.filter(any_changed)[id_col].to_list()
    examples = set(changed_ids) | set(removed[id_col].to_list())
    examples |= set(added[id_col].to_list())
    per_column.sort(key=lambda item: (-item[1], item[0]))
    return [
        Finding(
            component="features",
            as_of_date=check.as_of_date,
            cut=check.cut.describe(),
            added=added.height,
            removed=removed.height,
            changed=n_changed,
            examples=_examples(examples),
            columns=tuple(per_column),
        )
    ]


# --------------------------------------------------------------------------------------
# Database steps
# --------------------------------------------------------------------------------------


def _fetch(conn: psycopg.Connection[Any], query: str, what: str) -> list[Any]:
    """Run a parameterless statement and fetch every row; wrap a failure with context."""
    try:
        return conn.execute(_sql(query)).fetchall()
    except psycopg.Error as exc:
        raise LeakageCheckError(f"{what} failed: {exc}") from exc


def _require_open_transaction(conn: psycopg.Connection[Any], after: str) -> None:
    """Fail when the check's transaction is no longer open (something ended it)."""
    status = conn.info.transaction_status
    if status != pq.TransactionStatus.INTRANS:
        raise LeakageCheckError(
            f"the check's transaction is no longer open after {after} (status"
            + f" {status.name}). The cuts were undone, so the check cannot continue;"
            + " report this with the featurizer version installed."
        )


def _arm_commit_guard(conn: psycopg.Connection[Any]) -> None:
    """Make every COMMIT on ``conn`` fail for the rest of the transaction.

    A deferred foreign key on a temp table points at a missing row. Statements pass, and the
    check at COMMIT raises, so PostgreSQL rolls the whole transaction back: the cuts can
    never become permanent, whatever code runs on the connection. Needs only the TEMPORARY
    privilege on the database.
    """
    try:
        for statement in _COMMIT_GUARD:
            conn.execute(_sql(statement))
    except psycopg.Error as exc:
        raise LeakageCheckError(
            "could not create the check's temporary commit guard. The role needs the"
            + f" TEMPORARY privilege on the database: {exc}"
        ) from exc


def preflight_privileges(
    conn: psycopg.Connection[Any], sources: Sequence[SourceSpec]
) -> None:
    """Every cuttable Source must be a table the current role may DELETE from.

    Raises:
        LeakageCheckError: naming the relations that are missing, are not tables, or lack
            the DELETE privilege, and what to do about each.
    """
    absent: list[str] = []
    not_tables: list[str] = []
    no_delete: list[str] = []
    for source in sources:
        row = conn.execute(
            "select c.relkind, has_table_privilege(c.oid, 'DELETE')"
            + " from pg_class as c where c.oid = to_regclass(%s)",
            (_quote_relation(source.relation),),
        ).fetchone()
        if row is None:
            absent.append(source.relation)
        elif row[0] not in ("r", "p"):
            not_tables.append(source.relation)
        elif not row[1]:
            no_delete.append(source.relation)
    if absent:
        raise LeakageCheckError(
            f"declared Source relation(s) not found: {', '.join(absent)}."
            + " Fix sources[].relation in the experiment config."
        )
    if not_tables:
        raise LeakageCheckError(
            f"declared Source(s) {', '.join(not_tables)} are views or other non-table"
            + " relations, which the check cannot cut. Declare the base table(s) they"
            + " read, each with its knowledge_date_column."
        )
    if no_delete:
        user_row = conn.execute("select current_user").fetchone()
        user = user_row[0] if user_row else "the current role"
        raise LeakageCheckError(
            f"role {user!r} has no DELETE privilege on {', '.join(no_delete)}."
            + " The check deletes inside one transaction and always rolls back; it never"
            + " commits. Run it with a role that has DELETE on those Sources, such as the"
            + " role that loaded them."
        )


def delete_after(
    conn: psycopg.Connection[Any], sources: Sequence[SourceSpec], cut: CutKey
) -> dict[str, int]:
    """Delete every cuttable Source's rows that ``cut`` does not keep; return rows deleted.

    Event Sources are cut before entity Sources, so an event's foreign key to its entity
    holds. Each delete runs under a savepoint: a failure (a foreign key broken because a
    referencing row is known before the row it references, a lock held past
    ``lock_timeout``) is rolled back to it and raised as :class:`LeakageCheckError`.
    """
    deleted: dict[str, int] = {}
    for source in sorted(sources, key=lambda s: s.is_entity):
        if source.knowledge_date_column is None:
            continue
        op = ">" if cut.keeps_equal(source) else ">="
        statement = (
            f"delete from {_quote_relation(source.relation)}"
            + f" where {_quote_column(source.knowledge_date_column)} {op} %s"
        )
        with conn.cursor() as cur:
            cur.execute(_sql(f"savepoint {_DELETE_SAVEPOINT}"))
            try:
                cur.execute(_sql(statement), (cut.at,))
            except psycopg.Error as exc:
                cur.execute(_sql(f"rollback to savepoint {_DELETE_SAVEPOINT}"))
                raise LeakageCheckError(_delete_failure(source, cut, exc)) from exc
            deleted[source.name] = cur.rowcount
            cur.execute(_sql(f"release savepoint {_DELETE_SAVEPOINT}"))
    return deleted


def _delete_failure(source: SourceSpec, cut: CutKey, exc: psycopg.Error) -> str:
    what = (
        f"deleting the rows of Source {source.name!r} ({source.relation}) for the cut"
        + f" '{cut.describe()}' failed: {exc}"
    )
    if isinstance(exc, psycopg.errors.ForeignKeyViolation):
        return (
            what
            + ". A row of another table references a row of this Source and is known"
            + " before it. Declare the referencing table as a Source with its"
            + " knowledge_date_column (role: event) so it is cut first, or check that"
            + f" {source.knowledge_date_column} is when the row became known."
        )
    if isinstance(exc, psycopg.errors.LockNotAvailable):
        return (
            what
            + f". Another session holds locks on {source.relation} (lock_timeout"
            + f" {LOCK_TIMEOUT}). Run the check when no loader is writing to it."
        )
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return what + ". Run the check with a role that has DELETE on this Source."
    return what


def _explain_relations(
    conn: psycopg.Connection[Any], query: str, what: str
) -> set[tuple[str, str]]:
    """The base relations a query reads, from the planner (views are expanded)."""
    rows = _fetch(conn, f"explain (verbose, format json) {query}", f"planning {what}")
    found: set[tuple[str, str]] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, Mapping):
            name = node.get("Relation Name")
            if name is not None:
                found.add((str(node.get("Schema", "")), str(name)))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(rows[0][0] if rows else None)
    return found


def discover_relations(
    conn: psycopg.Connection[Any],
    plan: CutPlan,
    experiment: _Experiment,
    sources: Sequence[SourceSpec],
) -> list[NotChecked]:
    """Relations the cohort, labels or features read that no Source declares.

    Reads the planner's JSON plan for one rendered cohort query, one rendered label query,
    and a ``select *`` over each table featurizer reads (entity tables and
    ``graph_relationships`` edge tables), so views resolve to their base
    tables and no SQL is parsed (ADR-0014). Relations in the ``triage`` and system schemas
    are ignored. A declared Source counts through the base tables it reads, so a declared
    view covers the tables beneath it.
    """
    as_of_date = plan.as_of_dates[0]
    reads: dict[tuple[str, str], set[str]] = defaultdict(set)

    cohort_sql = render_cohort_query(experiment.cohort_query, as_of_date)
    for rel in _explain_relations(
        conn, f"select sub.entity_id from ({cohort_sql}) as sub", "the cohort query"
    ):
        reads[rel].add("cohort query")
    label_sql = render_label_query(
        experiment.label_query, as_of_date, plan.label_timespans[as_of_date][0]
    )
    for rel in _explain_relations(
        conn, f"select * from ({label_sql}) as sub", "the label query"
    ):
        reads[rel].add("label query")
    for table in experiment.featurizer_tables:
        for rel in _explain_relations(
            conn, f"select * from {table}", f"featurizer table {table}"
        ):
            reads[rel].add("featurizer config")

    declared: set[tuple[str, str]] = set()
    for source in sources:
        declared |= _explain_relations(
            conn,
            f"select * from {_quote_relation(source.relation)}",
            f"Source {source.name!r} ({source.relation})",
        )

    return [
        NotChecked(
            relation=f"{schema}.{name}",
            reason=f"read by the {', '.join(sorted(readers))} but not declared as a"
            + " Source: not cut, not checked",
        )
        for (schema, name), readers in sorted(reads.items())
        if (schema, name) not in declared
        and schema not in _SYSTEM_SCHEMAS
        and not schema.startswith("pg_")
    ]


# --------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Experiment:
    """The parts of the experiment config the check runs."""

    cohort_query: str
    label_query: str
    label_columns: tuple[str, ...]
    featurizer_config: Mapping[str, Any]
    #: The tables featurizer reads (entity tables and graph edge tables), quoted.
    featurizer_tables: tuple[str, ...]

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> _Experiment:
        """Validate the cohort, label and feature blocks; collect every error at once."""
        problem_type = config.get("problem_type")
        errors = [m for _, m in cohort_config_errors(config.get("cohort_config"))]
        errors += [
            m for _, m in label_config_errors(config.get("label_config"), problem_type)
        ]
        label_columns: tuple[str, ...] = ()
        try:
            label_columns = label_value_columns(str(problem_type))
        except ValueError as exc:
            errors.append(str(exc))
        raw_features = config.get("feature_config")
        feature_config: Mapping[str, Any] = (
            raw_features if isinstance(raw_features, Mapping) else {}
        )
        entities: list[Any] = list(feature_config.get("entities") or [])
        tables: list[str] = []
        if not entities:
            errors.append("feature_config must be a featurizer config with entities")
        for entity in entities:
            try:
                tables.append(_quote_relation(str(entity["table"])))
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"feature_config.entities: {entity!r}: {exc}")
        # featurizer also reads each native graph relationship's edge table.
        for graph in feature_config.get("graph_relationships") or []:
            try:
                tables.append(_quote_relation(str(graph["edges"]["table"])))
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"feature_config.graph_relationships: {graph!r}: {exc}")
        if errors:
            raise LeakageCheckError(
                "the experiment config is not valid: "
                + "; ".join(errors)
                + ". Run `triage analyze-config CONFIG` for the full list."
            )
        return cls(
            cohort_query=config["cohort_config"]["query"],
            label_query=config["label_config"]["query"],
            label_columns=label_columns,
            featurizer_config=_featurizer_only(feature_config),
            featurizer_tables=tuple(tables),
        )


class _FeatureBuilder:
    """featurizer, planned once, run on the check's connection for explicit cohort pairs."""

    def __init__(self, featurizer_config: Mapping[str, Any]) -> None:
        from featurizer import Featurizer

        config_yaml = _featurizer_config_yaml(featurizer_config, paired=True)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(config_yaml)
            config_path = handle.name
        try:
            self.featurizer = Featurizer(config_path, validate=True)
        except Exception as exc:
            raise LeakageCheckError(
                f"featurizer rejected feature_config: {exc}. Check it with"
                + " `triage analyze-config CONFIG --features '*'`."
            ) from exc
        finally:
            Path(config_path).unlink(missing_ok=True)
        if self.featurizer.target.id is None:
            raise LeakageCheckError("feature_config's target entity needs an id")
        self.keys = ("as_of_date", self.featurizer.target.id.name)

    def build(
        self, conn: psycopg.Connection[Any], as_of_date: date, entities: Iterable[Any]
    ) -> pl.DataFrame:
        """The features of ``entities`` at ``as_of_date``, sorted by the keys.

        Runs inside a savepoint that is rolled back, which drops the ``as_of_dates`` table and
        any temp shards featurizer created, and leaves the cuts in place.
        """
        import polars as pl

        pairs = [(as_of_date, entity) for entity in sorted(entities)]
        conn.execute(_sql(f"savepoint {_FEATURES_SAVEPOINT}"))
        try:
            with conn.cursor() as cur:
                _materialize_pairs(cur, pairs)
            table = self.featurizer.to_arrow(connection=conn, impute=True)
            _require_open_transaction(conn, "featurizer.to_arrow")
            if not hasattr(table, "column_names"):  # an OrderedDict of column groups
                table = _merge_arrow_groups(table, self.keys[1])
        except psycopg.Error as exc:
            message = (
                f"featurizer failed on the {len(pairs)} cohort pairs of {as_of_date}"
            )
            if "triage_leakage_guard" in str(exc):  # the commit guard fired
                message = (
                    "featurizer tried to COMMIT the check's transaction. The commit guard"
                    + " refused it, so no Source was changed, but this featurizer version"
                    + " cannot run the check: pin the version the tripwire test passes on"
                )
            raise LeakageCheckError(f"{message}: {exc}") from exc
        finally:
            if conn.info.transaction_status in (
                pq.TransactionStatus.INTRANS,
                pq.TransactionStatus.INERROR,
            ):
                conn.execute(_sql(f"rollback to savepoint {_FEATURES_SAVEPOINT}"))
                conn.execute(_sql(f"release savepoint {_FEATURES_SAVEPOINT}"))
        frame = pl.from_arrow(table)
        assert isinstance(frame, pl.DataFrame)  # from_arrow(Table) is a frame
        return frame.sort(list(self.keys))


def _cohort(
    conn: psycopg.Connection[Any], experiment: _Experiment, as_of_date: date
) -> frozenset[Any]:
    rendered = render_cohort_query(experiment.cohort_query, as_of_date)
    rows = _fetch(
        conn,
        f"select sub.entity_id from ({rendered}) as sub",
        f"the cohort query at {as_of_date}",
    )
    return frozenset(row[0] for row in rows)


def _labels(
    conn: psycopg.Connection[Any],
    experiment: _Experiment,
    as_of_date: date,
    label_timespan: str,
) -> dict[Any, tuple[tuple[Any, ...], ...]]:
    rendered = render_label_query(experiment.label_query, as_of_date, label_timespan)
    values = ", ".join(f"sub.{column}" for column in experiment.label_columns)
    rows = _fetch(
        conn,
        f"select sub.entity_id, {values} from ({rendered}) as sub",
        f"the label query at {as_of_date} ({label_timespan})",
    )
    grouped: dict[Any, list[tuple[Any, ...]]] = defaultdict(list)
    for row in rows:
        grouped[row[0]].append(tuple(row[1:]))
    return {entity: tuple(sorted(v, key=repr)) for entity, v in grouped.items()}


@dataclass
class _Baseline:
    cohorts: dict[date, frozenset[Any]] = field(default_factory=dict)
    labels: dict[tuple[date, str], dict[Any, tuple[tuple[Any, ...], ...]]] = field(
        default_factory=dict
    )
    feature_files: dict[date, Path] = field(default_factory=dict)


def _build_baseline(
    conn: psycopg.Connection[Any],
    plan: CutPlan,
    experiment: _Experiment,
    features: _FeatureBuilder,
    workdir: Path,
) -> _Baseline:
    """Every artifact on all data. Features go to one Parquet file per date."""
    baseline = _Baseline()
    for as_of_date in plan.as_of_dates:
        cohort = _cohort(conn, experiment, as_of_date)
        baseline.cohorts[as_of_date] = cohort
        for label_timespan in plan.label_timespans[as_of_date]:
            baseline.labels[(as_of_date, label_timespan)] = _labels(
                conn, experiment, as_of_date, label_timespan
            )
        if cohort:
            path = workdir / f"features-{as_of_date.isoformat()}.parquet"
            features.build(conn, as_of_date, cohort).write_parquet(path)
            baseline.feature_files[as_of_date] = path
        logger.info(
            f"leakage check: baseline at {as_of_date} — {len(cohort)} cohort entities"
        )
    return baseline


def _observe(
    conn: psycopg.Connection[Any],
    check: Check,
    experiment: _Experiment,
    features: _FeatureBuilder,
    baseline: _Baseline,
) -> list[Finding]:
    """Rebuild one artifact after its cut and compare it with the baseline."""
    import polars as pl

    if check.component == "cohort":
        observed = _cohort(conn, experiment, check.as_of_date)
        return compare_cohort(check, baseline.cohorts[check.as_of_date], observed)
    if check.component == "labels":
        assert check.label_timespan is not None
        observed_labels = _labels(
            conn, experiment, check.as_of_date, check.label_timespan
        )
        return compare_labels(
            check,
            baseline.labels[(check.as_of_date, check.label_timespan)],
            observed_labels,
        )
    path = baseline.feature_files.get(check.as_of_date)
    if path is None:  # an empty cohort has no features to compare
        return []
    observed_features = features.build(
        conn, check.as_of_date, baseline.cohorts[check.as_of_date]
    )
    return compare_features(
        check, pl.read_parquet(path), observed_features, features.keys
    )


def check_leakage(
    db_url: str, config: Mapping[str, Any], dates: Sequence[date] | None = None
) -> LeakageReport:
    """Run the leakage check on the project database; change nothing (ADR-0034).

    Opens its own connection (not the pool) and runs every cut inside one transaction that
    always ends in ROLLBACK. See the module docstring for the property tested.

    Args:
        db_url: the project-database URL (``postgresql[+psycopg]://…``).
        config: the experiment config (cohort_config, label_config, feature_config,
            temporal_config, problem_type, sources).
        dates: check only these as_of_dates. Default: every as_of_date.

    Returns:
        The report: findings (empty when no leakage), relations not checked, dates checked
        and the elapsed time.

    Raises:
        LeakageCheckError: when the check cannot run (bad config, no cuttable Source,
            missing privilege, a broken foreign key, a held lock), or when the Source
            fingerprints differ after the rollback.
    """
    started = time.monotonic()
    experiment = _Experiment.from_config(config)
    plan = plan_cuts(config, dates)
    sources = declared_sources(config)
    cuttable, uncut = cuttable_sources(sources)
    if not cuttable:
        raise LeakageCheckError(
            "no declared Source has a knowledge_date_column, so there is nothing to cut."
            + " Declare the tables the cohort, label and feature queries read under"
            + " sources:, each with the column that says when a row became known."
        )
    features = _FeatureBuilder(experiment.featurizer_config)
    conninfo = libpq_conninfo(db_url)

    before = fingerprints(conninfo, cuttable)
    report = LeakageReport(
        dates_checked=list(plan.as_of_dates),
        label_checks=[
            (check.as_of_date, check.label_timespan)
            for check in plan.checks
            if check.label_timespan is not None
        ],
        sources_cut=[source.name for source in cuttable],
    )
    report.not_checked.extend(
        NotChecked(
            relation=source.relation,
            source=source.name,
            reason="declared without a knowledge_date_column: not cut, not checked",
        )
        for source in uncut
    )

    try:
        conn = psycopg.connect(conninfo, autocommit=False)
    except psycopg.Error as exc:
        raise LeakageCheckError(
            f"could not connect to the project database: {exc}"
        ) from exc
    try:
        conn.execute(_sql(f"set local lock_timeout = '{LOCK_TIMEOUT}'"))
        _arm_commit_guard(conn)
        preflight_privileges(conn, cuttable)
        report.not_checked.extend(discover_relations(conn, plan, experiment, sources))
        with tempfile.TemporaryDirectory(prefix="triage-leakage-") as workdir:
            baseline = _build_baseline(conn, plan, experiment, features, Path(workdir))
            for cut in plan.cuts:
                deleted = delete_after(conn, cuttable, cut)
                logger.info(
                    f"leakage check: cut '{cut.describe()}' — deleted {deleted}"
                )
                for check in plan.checks_at(cut):
                    report.add(_observe(conn, check, experiment, features, baseline))
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()

    after = fingerprints(conninfo, cuttable)
    if after != before:  # cannot happen after a rollback; fail loudly if it does
        raise LeakageCheckError(
            f"Source fingerprints changed during the check: {before} -> {after}."
            + " Another session may have loaded data meanwhile; re-run the check, and"
            + " report it if the Sources were idle."
        )
    report.elapsed_s = time.monotonic() - started
    return report
