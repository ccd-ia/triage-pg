/*
 * ConfigPanel — the experiment's recipe + provenance, on the experiment detail screen.
 * Renders the stored experiments.config (the thing the experiment_hash is computed over):
 * temporal configuration, the model grid, the feature config, and the cohort/label queries —
 * plus a build summary (built vs reused models) and a timeline of the experiment's runs.
 */
import { useState } from 'react'
import type {
  ExperimentAttempt,
  ExperimentConfig,
  ExperimentSummary,
  ModelReuse,
  RunListItem,
} from '../api/types'
import { StatusBadge } from './StatusBadge'

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function fmtVal(v: unknown): string {
  if (v == null) return '—'
  if (Array.isArray(v)) return v.join(', ')
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function KvGrid({ obj }: { obj: Record<string, unknown> }) {
  return (
    <div className="kv">
      {Object.entries(obj).map(([k, v]) => (
        <span key={k} style={{ display: 'contents' }}>
          <span className="k2">{k}</span>
          <span className="v2 mono">{fmtVal(v)}</span>
        </span>
      ))}
    </div>
  )
}

/** A collapsible <pre> for long values (SQL queries, raw feature config). */
function Collapsible({ title, body }: { title: string; body: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button type="button" className="seg" onClick={() => setOpen((o) => !o)}>
        {open ? '▾' : '▸'} {title}
      </button>
      {open ? (
        <pre className="codeblock">{body}</pre>
      ) : null}
    </div>
  )
}

function leaf(classPath: string): string {
  return classPath.split('.').pop() ?? classPath
}

export function ConfigPanel({
  config,
  attempt,
  summary,
  modelReuse,
  runs,
  activeRunId,
  onSelectRun,
}: {
  config: ExperimentConfig | null
  /** the active run's attempt (feature/grid/imputation) — ADR-0022; these vary per run. */
  attempt?: ExperimentAttempt | null
  summary: ExperimentSummary
  modelReuse: ModelReuse
  runs: RunListItem[]
  activeRunId?: string
  onSelectRun: (runId: string) => void
}) {
  const c = (config ?? {}) as Record<string, unknown>
  // The experiment carries the PROBLEM (cohort/label/temporal); the grid + features belong to
  // the RUN's attempt (ADR-0022). Fall back to config for pre-0022 experiment rows.
  const temporal = asRecord(c.temporal_config)
  const grid = asRecord(attempt?.grid_config) ?? asRecord(c.grid_config)
  const features = asRecord(attempt?.feature_config) ?? asRecord(c.feature_config)
  const fromAttempt = !!asRecord(attempt?.grid_config)
  const cohort = asRecord(c.cohort_config)
  const label = asRecord(c.label_config)

  return (
    <div className="configpanel">
      {/* build summary */}
      <section className="card">
        <div className="ch">
          <b>Build</b>
          <span className="src">experiment_actuals · model_reuse</span>
        </div>
        <div className="strip">
          <Cell label="model groups" value={`${summary.n_model_groups}`} />
          <Cell label="models built" value={`${modelReuse.built}`} />
          <Cell label="models reused" value={`${modelReuse.reused}`} />
          <Cell label="splits" value={`${summary.n_splits}`} />
          <Cell label="features" value={summary.n_features != null ? `${summary.n_features}` : '—'} />
        </div>
        {modelReuse.reused > 0 ? (
          <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
            {modelReuse.reused} of {modelReuse.built + modelReuse.reused} models were reused from
            another run's cache (cache-share, ADR-0013–0017) — only {modelReuse.built} were built here.
          </div>
        ) : null}
      </section>

      {/* temporal configuration */}
      {temporal ? (
        <section className="card">
          <div className="ch">
            <b>Temporal configuration</b>
            <span className="src">config.temporal_config</span>
          </div>
          <KvGrid obj={temporal} />
        </section>
      ) : null}

      {/* model grid */}
      {grid ? (
        <section className="card">
          <div className="ch">
            <b>Model grid</b>
            <span className="src">{fromAttempt ? 'run.plan.attempt · grid_config' : 'config.grid_config'}</span>
          </div>
          {Object.entries(grid).map(([classPath, params]) => (
            <div key={classPath} style={{ marginBottom: 8 }}>
              <div className="mono" style={{ fontSize: 11.5, color: 'var(--acc)' }}>{leaf(classPath)}</div>
              {asRecord(params) ? <KvGrid obj={asRecord(params)!} /> : null}
            </div>
          ))}
        </section>
      ) : null}

      {/* features */}
      {features ? (
        <section className="card">
          <div className="ch">
            <b>Features</b>
            <span className="src">{fromAttempt ? 'run.plan.attempt · feature_config' : 'config.feature_config'}</span>
          </div>
          <div className="muted" style={{ fontSize: 11, marginBottom: 6 }}>
            {summary.n_features != null ? `${summary.n_features} features` : 'feature engine (featurizer) config'}
            {Array.isArray(features.entities) ? ` · ${(features.entities as unknown[]).length} entities` : ''}
            {Array.isArray(features.relationships)
              ? ` · ${(features.relationships as unknown[]).length} relationships`
              : ''}
          </div>
          <Collapsible title="raw feature config" body={JSON.stringify(features, null, 2)} />
        </section>
      ) : null}

      {/* cohort / label */}
      {(cohort || label) ? (
        <section className="card">
          <div className="ch">
            <b>Cohort &amp; label</b>
            <span className="src">config.cohort_config · label_config</span>
          </div>
          {cohort ? (
            <Collapsible
              title={`cohort${cohort.name ? ` · ${fmtVal(cohort.name)}` : ''}`}
              body={fmtVal(cohort.query ?? cohort)}
            />
          ) : null}
          {label ? (
            <Collapsible
              title={`label${label.name ? ` · ${fmtVal(label.name)}` : ''}`}
              body={fmtVal(label.query ?? label)}
            />
          ) : null}
        </section>
      ) : null}

      {/* run timeline */}
      <section className="card">
        <div className="ch">
          <b>Runs · timeline</b>
          <span className="src">triage.runs</span>
        </div>
        <div className="timeline">
          {runs.map((r) => (
            <button
              key={r.run_id}
              type="button"
              className={`tl-run${r.run_id === activeRunId ? ' sel' : ''}`}
              onClick={() => onSelectRun(r.run_id)}
            >
              <span className={`tl-dot tl-${r.status}`} />
              <span className="tl-main">
                <span className="mono">{r.run_id.slice(0, 8)}</span>
                <span className="badge b-aud" style={{ marginLeft: 6 }}>{r.purpose ?? 'experiment'}</span>
                <StatusBadge status={r.status} />
              </span>
              <span className="tl-meta muted">
                {r.started_at?.slice(0, 19).replace('T', ' ') ?? '—'}
                {r.git_hash ? ` · ${r.git_hash.slice(0, 7)}` : ''}
                {r.profile ? ` · ${r.profile}` : ''}
              </span>
            </button>
          ))}
        </div>
      </section>
    </div>
  )
}

function Cell({ label, value }: { label: string; value: string }) {
  return (
    <div className="cell">
      <span className="lbl">{label}</span>
      <span className="val num">{value}</span>
    </div>
  )
}
