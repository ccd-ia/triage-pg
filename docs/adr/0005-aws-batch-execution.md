# 0005. Experiment execution via AWS Batch (one job per experiment)

- Status: Accepted
- Date: 2026-06-04
- Status update (2026-06-28): Implemented (code), not yet exercised on live AWS — `profiles/execution.py` `BatchExecution` (one Batch job per experiment); rq + multicore orchestration removed.

In the cloud profile, each experiment runs as **one AWS Batch job** (the triage-pg container + config + the target project's connection); cross-experiment parallelism is Batch's queue, and grid-search parallelism stays as multiprocessing inside the container. Batch becomes the distributed runner, so **rq and the multicore-orchestration code are removed**. The local profile runs the same experiment in-process / in a local container.

## Considered alternatives
- *Keep rq / MultiCoreExperiment* — rejected: removing them is an explicit simplification goal; Batch subsumes distributed execution.
- *One Batch job per model-group (Batch array jobs)* — deferred: a later optimization; one-job-per-experiment is the simple starting point.

## Consequences
- Ephemeral jobs need credentials delivered at launch → see IAM auth (ADR-0004).
- Compute is ephemeral → durable state must be external (Parquet/S3 + the project PG).
