# Cloud-profile spec — the auth / storage / execution adapter seam

triage-pg isolates every environment-specific concern behind three small swappable
adapters (ADR-0003): **auth**, **storage**, **execution**. Until now the `--profile`
flag was inert — stored on `triage.runs.profile` but changing nothing; the local path
(standalone PG + password + local FS + in-process) was hard-wired. This document
specifies the seam and the **cloud** profile (RDS+IAM + S3 + AWS Batch, ADR-0004/0005),
analogous to `docs/adapter-spec.md` for the featurizer seam.

| # | Adapter | Local | Cloud | Status |
|---|---------|-------|-------|--------|
| 1 | auth | static password URL | RDS IAM, per-connect token | **implemented** (`triage/profiles/auth.py`) |
| 2 | storage | local FS (`Path`) | S3 via s3fs/pyarrow | **implemented** (`triage/profiles/storage.py`) |
| 3 | execution | in-process `run_experiment` | one AWS Batch job/experiment | **implemented** (`triage/profiles/execution.py`) |

> **Implementation note (2026-06-21).** Built per this spec. The `Profile` value object +
> `load_profile` live in `triage/profiles/__init__.py`; the three `Protocol`s in
> `triage/profiles/protocols.py`. Two deviations from the spec letter, both deliberate:
> (a) **Parquet/joblib IO over S3 routes through `s3fs` (file-handle) rather than
> `pyarrow.fs.S3FileSystem`** — pyarrow's native C++ S3 SDK is *not* intercepted by `moto`
> (it bypasses botocore), so the spec's mocked-AWS testing contract (§6) is only satisfiable
> via the botocore-backed `s3fs` path; this also keeps one IO code path for both schemes.
> `StorageAdapter.filesystem()` still returns the pyarrow fs for callers that want native access.
> (b) **The score/forward read paths keep their existing signatures** (no `storage` param added):
> `_design_X` / `_load_estimator` derive the adapter from the artifact URI scheme via
> `storage_for_root`, matching how GC dispatches by `output_ref`. The one core signature change is
> exactly as specified — `run_experiment`'s `storage_dir: str` → `storage` + `storage_root`,
> threaded through `_build_split` → `build_matrix` / `build_model`. Tests:
> `src/tests/test_profiles.py` (12, mocked-AWS); `moto` was bumped 3.1.7 → 5.x for `mock_aws`.
> Old `catwalk/storage.py` was **not** retired — `cli.py`'s `Store.factory` (config/YAML loading)
> and several `catwalk_tests` still import it; retiring it is out of scope for this seam.

**Scope (decided 2026-06-20):** spec **and** build both profiles now, with a testing
contract of mocked AWS (`moto`/`localstack`) + a stubbed token provider — no live-AWS
dependency in the suite (§6). The local-PG suite (`pytest-postgresql`/CI) stays the
non-negotiable substrate (ADR-0003); cloud is always *additive*.

## Resolved decisions

1. **Profile is a value object that wraps the core** (§1). A frozen `Profile` bundling
   `{auth, storage, execution}`, built once at the CLI boundary from `--profile` + env.
   `auth` produces the pool and `execution` wraps submit-or-run *around* `run_experiment`;
   only `storage` threads *into* it — `run_experiment` stays execution-/auth-agnostic.
2. **IAM auth refreshes the token per physical connection** (§2) via a custom psycopg
   `connection_class`; no timer. A token only has to be valid at connect time.
3. **Storage is a new fsspec/pyarrow scheme adapter** (§3) that absorbs GC's existing
   `_delete_output_file`; the old `catwalk/storage.py` `Store` is retired.
4. **Execution is a thin submit/run switch** (§4); cloud uploads the config to S3 and
   `submit_job`s against pre-provisioned Batch infra, the container authenticating via its
   IAM **task role** (no credentials are ever passed).
5. **Cloud parameters come from environment variables** (§5), fail-fast under
   `--profile cloud`.
6. **Testing is mocked-AWS** (§6): `moto`/`localstack` + a stubbed token provider.
7. **Grid parallelism stays serial for now** — deferred to a new ADR-0020 (§8).

---

## 1. The `Profile` bundle and dispatch

A frozen value object, the single seam the CLI constructs and the only place that knows
which environment we're in:

```python
# src/triage/profiles/__init__.py  (new package)
@dataclass(frozen=True)
class Profile:
    name: str                  # 'local' | 'cloud'
    auth: AuthAdapter          # produces the ConnectionPool
    storage: StorageAdapter    # read/write/delete artifact bytes by URI scheme
    execution: ExecutionAdapter  # in-process vs Batch submit

def load_profile(name: str) -> Profile: ...   # 'local' | 'cloud' → built from env
```

The three adapters are **Protocols** (structural; trivially stubbable in tests):

```python
class AuthAdapter(Protocol):
    def open_pool(self, *, min_size: int = 1, max_size: int = 10) -> ConnectionPool: ...

class StorageAdapter(Protocol):
    def join(self, *parts: str) -> str: ...               # build a child URI
    def write_bytes(self, uri: str, data: bytes) -> None: ...
    def open_output(self, uri: str): ...                  # context-managed writable file obj
    def open_input(self, uri: str): ...                   # context-managed readable file obj
    def filesystem(self):  ...                            # pyarrow.fs / fsspec handle for Parquet
    def delete(self, uri: str) -> bool: ...               # absorbs GC _delete_output_file

class ExecutionAdapter(Protocol):
    def run(self, experiment_config: Mapping, *, storage_root: str, **kw) -> RunHandle: ...
```

**Where each adapter touches the flow (per decision 1 — minimal blast radius on the
tested core):**

- `auth` is used by the CLI *before* `run_experiment`: `pool = profile.auth.open_pool()`.
  `run_experiment` keeps taking a constructed `ConnectionPool` (unchanged signature).
- `execution` wraps *around* `run_experiment`. The local adapter calls it in-process and
  returns immediately with the `RunResult`. The cloud adapter `submit_job`s and returns a
  `RunHandle` carrying the Batch `job_id` (async; see §4). `run_experiment` itself does not
  change for execution.
- `storage` threads *into* `run_experiment`: the current `storage_dir: str` parameter
  becomes a `storage: StorageAdapter` + a `storage_root: str` URI. The matrix/model
  builders (`adapters/matrix.py`, `adapters/model.py`) write/read through it instead of
  `Path(...)`. GC (`artifacts.delete_outputs`) calls `storage.delete` instead of the
  module-level `_delete_output_file`.

This is the **only** change to `run_experiment`'s signature: `storage_dir: str` →
`(storage: StorageAdapter, storage_root: str)`. Everything else about the headless core
is untouched, so its unit tests run under a trivial local `StorageAdapter` over `tmp_path`.

---

## 2. Auth adapter

### 2.1 Local
`LocalAuth.open_pool()` is today's `util/db.connection_pool(dburl, ...)` verbatim — a
static password conninfo resolved by `cli.resolve_db_url` (`DATABASE_URL` / `PG*` /
`database.yaml`). No change in behavior.

### 2.2 Cloud (RDS IAM)
The password is a short-lived (~15 min) token from `boto3` RDS
`generate_db_auth_token`. The pool's conninfo is fixed at construction, so a fresh token
is injected **per new physical connection** via a custom `connection_class` — relying on
the fact that **a token only needs to be valid at connect time**; once a connection
authenticates, the session persists regardless of token expiry.

```python
class _IamConnection(psycopg.Connection):
    @classmethod
    def connect(cls, conninfo="", **kwargs):
        token = _token_provider()          # boto3 rds.generate_db_auth_token(...)
        kwargs["password"] = token
        kwargs.setdefault("sslmode", "verify-full")
        kwargs.setdefault("sslrootcert", _RDS_CA_BUNDLE)
        return super().connect(conninfo, **kwargs)

class CloudAuth:
    def open_pool(self, *, min_size=1, max_size=10) -> ConnectionPool:
        return ConnectionPool(
            _base_conninfo(),              # host/port/db/user from env, NO password
            connection_class=_IamConnection,
            kwargs={"row_factory": dict_row},
            max_lifetime=600,              # recycle within ~token TTL, defensive
            min_size=min_size, max_size=max_size, open=True,
        )
```

- `_token_provider` is an injectable seam (stubbed in tests, §6); the real one is
  `boto3.client("rds").generate_db_auth_token(host, port, user, Region=...)`.
- `verify-full` + the RDS CA bundle are mandatory (IAM auth requires TLS).
- `max_lifetime=600` recycles pooled connections so none drifts far past a token TTL —
  defensive only; correctness does not depend on it.

---

## 3. Storage adapter

A small adapter over **fsspec/s3fs + `pyarrow.fs`**, dispatching local-vs-S3 by URI
scheme. It replaces both the plain-`Path` writes in the builders **and** GC's
`_delete_output_file`, giving one storage seam. The inherited `catwalk/storage.py`
`Store`/`FSStore`/`S3Store`/`ProjectStorage` (CSV-era, tmp-download read path) is retired.

- **Local** (`scheme in ('', 'file')`): `pyarrow.fs.LocalFileSystem`; `delete` = today's
  `Path.unlink` branch.
- **S3** (`scheme == 's3'`): `pyarrow.fs.S3FileSystem` (or `s3fs`); credentials resolve via
  the standard AWS chain — the Batch task role in cloud, the dev's env locally for testing.
- **Parquet**: `pyarrow.parquet.write_table(table, where, filesystem=storage.filesystem())`
  writes straight to `s3://…` — no `/tmp` round-trip. `model.py`'s joblib dump goes through
  `storage.open_output(uri)`.
- `storage_root` is a URI: `./matrices` locally, `s3://$TRIAGE_S3_BUCKET/<scope>` in cloud.
  Layout is unchanged (`<uuid5(artifact_id)>.parquet`, `<…>.joblib`); only the root scheme
  differs. `output_ref` on `triage.artifacts` remains the full URI (already scheme-aware).

`artifacts.delete_outputs` becomes `delete_outputs(storage, external)` and calls
`storage.delete(ref)`; the feature_group sentinel skip (just added) is unchanged.

---

## 4. Execution adapter

A thin **submit-or-run switch**; it does not restructure `run_experiment`.

- **Local** (`InProcessExecution`): calls `run_experiment(pool, config, storage=…,
  storage_root=…, …)` synchronously and returns a `RunHandle(run_result=…)`.
- **Cloud** (`BatchExecution`): one Batch job per experiment (ADR-0005).
  1. Serialize `experiment_config` (canonical JSON) and `storage.write_bytes` it to
     `s3://$TRIAGE_S3_BUCKET/<scope>/config.json`.
  2. `boto3.client("batch").submit_job(jobQueue=$TRIAGE_BATCH_QUEUE,
     jobDefinition=$TRIAGE_BATCH_JOB_DEF, parameters={config_uri, profile: 'cloud', …})`.
  3. Return `RunHandle(batch_job_id=…)` **immediately** — the CLI does **not** block on a
     Batch job (async-submit-and-return; the operator polls). The `job_id` is recorded on
     `triage.runs.batch_job_id` (and `registry.submissions.batch_job_id` when the registry
     lands).
  4. The **container entrypoint** runs `triage run --profile cloud --config <config_uri>`,
     which `load_profile('cloud')` → CloudAuth pool + S3 storage + InProcessExecution
     (inside the job it runs in-process), reading the config from S3.

**Credentials never cross the wire**: the Batch job's container assumes its IAM **task
role**, which grants RDS-IAM-connect + S3 read/write. The same role makes CloudAuth's
`generate_db_auth_token` and the S3 storage adapter work with zero passed secrets.

**Infra is out of band**: the queue, compute environment, job definition, and task role
are a documented prerequisite provisioned via Terraform/console (the `terraform-skill`
applies), **not** created by the app (rejected: adapter-provisions-infra couples the run
path to AWS resource lifecycle). The app only `submit_job`s against names from §5.

---

## 5. Cloud configuration (environment variables)

`--profile` selects local vs cloud; cloud parameters come from the environment (matches
the project's `PG*`/`DATABASE_URL` discipline and the CLAUDE.md hard rule; direnv-friendly;
no new file format). Under `--profile cloud`, a missing required var is a **fail-fast**
error naming the var (no silent default — CLAUDE.md error policy).

| Var | Purpose |
|-----|---------|
| `AWS_REGION` | RDS token region + boto3 clients |
| `TRIAGE_RDS_HOST`, `TRIAGE_RDS_PORT`, `TRIAGE_RDS_DB`, `TRIAGE_RDS_USER` | IAM conninfo (no password) |
| `TRIAGE_S3_BUCKET` | storage root + config staging |
| `TRIAGE_BATCH_QUEUE`, `TRIAGE_BATCH_JOB_DEF` | `submit_job` targets |

(IAM task role and CA bundle are environmental/AMI concerns, not app config.)

---

## 6. Testing contract (no live AWS)

"Build cloud too" counts as done only when the cloud adapters are tested **without** live
AWS:

- **auth**: inject a stub `_token_provider` returning a fixed string; assert the
  `connection_class` puts it in `password` + sets `sslmode=verify-full`. (No real RDS.)
- **storage**: run the FS branch against `tmp_path`; run the S3 branch against `moto`
  (`mock_aws`) — write a Parquet, read it back, `delete` it, assert the scheme dispatch.
- **execution**: `moto` Batch + S3 — `BatchExecution.run` writes the config to (mock) S3
  and calls `submit_job`; assert the job parameters carry the config URI and `profile`.
- **profile**: `load_profile('cloud')` with the env vars set (monkeypatched) builds a
  `Profile` whose three adapters are the cloud impls; missing-var raises with the var name.

The existing `pytest-postgresql` suite is unaffected (local profile is the default).

---

## 7. Open / deferred

- **Grid parallelism** — in-container multiprocessing (ADR-0005) stays **deferred**; the
  grid×split loop remains serial. Rationale + the fork/pool/pickle hazard recorded in
  **ADR-0020** (§8).
- **Registry-driven routing** — the cloud profile reads its target from env, not the
  registry (multi-project deferred). When ADR-0002's control plane lands, `load_profile`
  can additionally resolve per-project routing from the registry.
- **Batch job monitoring / status backfill** — the CLI returns the `job_id`; a
  `triage runs status` poll command and writing the Batch terminal state back onto
  `triage.runs` is a thin follow-on (post-submit), not part of this seam.

---

## 8. Deferred: grid parallelism → ADR-0020

See `docs/adr/0020-defer-in-container-grid-parallelism.md`. Summary: ADR-0005 envisioned
grid-search parallelism as in-container multiprocessing, but the loop is serial today and
naive multiprocessing re-introduces the cross-fork DB-pool sharing + estimator pickling
that removing `SerializableDbEngine` (ADR-0019) just eliminated. Parallelism is a perf
knob, not correctness; deferred until a real throughput need, then designed deliberately
(worker-owns-its-own-pool, matrix re-loaded from storage, no shared connection objects).
