/*
 * SurvivalPanels — the model card's survival section, over the set `triage postmodel
 * survival MODEL --horizon H` persists (/models/{id}/survival, migration 0036). It reads
 * the horizon computed most recently and its latest date:
 *   - Uno's C at the horizon and the log-rank test across the risk tertiles;
 *   - KM by risk decile: the Kaplan-Meier probability of an event by the horizon in each
 *     decile of risk (1 = the riskiest), against the population's;
 *   - Brier(t) over the time grid, beside the Brier of the training Kaplan-Meier (a model
 *     that knows nothing of the features), and AUC(t);
 *   - calibration at the horizon: per decile of predicted survival, the mean predicted S(H)
 *     against the KM-observed S(H), with ICI and E50.
 * A value the diagnostics could not define is null, and its reason is shown.
 */
import {
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { fmtNum, fmtPct, tooltipFormatter } from '../api/format'
import type { SurvivalRow } from '../api/types'
import { AsyncPanel } from './AsyncPanel'

const TOOLTIP = { background: 'var(--panel)', border: '1px solid var(--line)', fontSize: 11 }

/** The reason a value is undefined, as the diagnostics stored it. */
function why(row: SurvivalRow | undefined): string | null {
  const reason = row?.extra?.undefined
  return typeof reason === 'string' ? reason : null
}

function num(row: SurvivalRow | undefined, key: string): number | null {
  const v = row?.extra?.[key]
  return typeof v === 'number' ? v : null
}

export function SurvivalPanels({ modelId }: { modelId: number }) {
  const survival = useAsync(() => api.modelSurvival(modelId), [modelId])
  return (
    <AsyncPanel state={survival} what="the survival diagnostics">
      {(data) => {
        const { kinds } = data
        const uno = kinds.uno_c[0]
        const logrank = kinds.logrank[0]
        const population = kinds.km_decile.find((r) => r.bin === 0)
        const deciles = kinds.km_decile
          .filter((r) => r.bin > 0)
          .map((r) => ({ decile: `d${r.bin}`, event: r.value, rows: r.n }))
        const auc = new Map(kinds.auc_t.map((r) => [r.t, r.value]))
        const brier = kinds.brier.map((r) => ({
          t: r.t,
          Brier: r.value,
          'training KM': num(r, 'km_reference'),
          'AUC(t)': auc.get(r.t) ?? null,
        }))
        const calSummary = kinds.calibration.find((r) => r.bin === 0)
        const calibration = kinds.calibration
          .filter((r) => r.bin > 0)
          .map((r) => ({ decile: `d${r.bin}`, predicted: num(r, 'predicted'), observed: r.value }))
        return (
          <>
            <section>
              <h4>survival · horizon {fmtNum(data.horizon, 2)} at {data.as_of_date}</h4>
              <div className="strip">
                <div className="cell" title={why(uno) ?? 'concordance over the comparable pairs before the horizon'}>
                  <span className="lbl">Uno's C</span>
                  <span className="val">{fmtNum(uno?.value, 3)}</span>
                </div>
                <div
                  className="cell"
                  title={why(logrank) ?? `chi-square ${fmtNum(num(logrank, 'chisq'), 2)} across the risk tertiles`}
                >
                  <span className="lbl">log-rank p</span>
                  <span className="val">
                    {logrank?.value != null && logrank.value < 1e-4
                      ? logrank.value.toExponential(1)
                      : fmtNum(logrank?.value, 4)}
                  </span>
                </div>
                <div className="cell" title="Kaplan-Meier probability of an event by the horizon, all rows">
                  <span className="lbl">P(event ≤ H)</span>
                  <span className="val">{fmtPct(population?.value)}</span>
                </div>
                <div className="cell" title={why(calSummary) ?? 'row-weighted mean |predicted − observed S(H)| over the deciles'}>
                  <span className="lbl">ICI</span>
                  <span className="val">{fmtNum(num(calSummary, 'ici'), 3)}</span>
                </div>
              </div>
              {[uno, logrank].map((r) => (why(r) ? (
                <div key={`${r?.bin}-${r?.t}`} className="muted" style={{ fontSize: 10.5 }}>{why(r)}</div>
              ) : null))}
            </section>

            <section>
              <h4>KM event probability by the horizon · per risk decile</h4>
              <div style={{ height: 140 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={deciles} margin={{ top: 6, right: 8, bottom: 0, left: -18 }}>
                    <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
                    <XAxis dataKey="decile" stroke="var(--mut)" tick={{ fontSize: 9 }} />
                    <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} domain={[0, 1]} />
                    <Tooltip contentStyle={TOOLTIP} formatter={tooltipFormatter(3)} />
                    {population?.value != null ? (
                      <ReferenceLine y={population.value} stroke="var(--mut)" strokeDasharray="4 3" />
                    ) : null}
                    <Bar dataKey="event" fill="var(--acc)" isAnimationActive={false} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="muted" style={{ fontSize: 10.5 }}>
                d1 = the riskiest tenth; dashed: the population · Kaplan-Meier, so a row censored
                before the horizon counts as at risk until it leaves
              </div>
            </section>

            <section>
              <h4>Brier(t) and AUC(t)</h4>
              {brier.length ? (
                <div style={{ height: 150 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={brier} margin={{ top: 6, right: 10, bottom: 0, left: -18 }}>
                      <CartesianGrid stroke="var(--line2)" strokeDasharray="3 3" />
                      <XAxis dataKey="t" stroke="var(--mut)" tick={{ fontSize: 9 }} />
                      <YAxis stroke="var(--mut)" tick={{ fontSize: 9 }} domain={[0, 1]} />
                      <Tooltip contentStyle={TOOLTIP} formatter={tooltipFormatter(3)} />
                      <Legend wrapperStyle={{ fontSize: 10 }} />
                      <Line dataKey="Brier" stroke="var(--acc)" strokeWidth={2} isAnimationActive={false} connectNulls={false} />
                      <Line dataKey="training KM" stroke="var(--mut)" strokeDasharray="4 3" isAnimationActive={false} />
                      <Line dataKey="AUC(t)" stroke="var(--ok)" isAnimationActive={false} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <div className="muted" style={{ fontSize: 11 }}>no Brier(t) row persisted</div>
              )}
              {kinds.brier.some((r) => why(r)) ? (
                <div className="muted" style={{ fontSize: 10.5 }}>
                  undefined at some times: {why(kinds.brier.find((r) => why(r)))}
                </div>
              ) : null}
            </section>

            <section>
              <h4>calibration at the horizon · predicted against observed S(H)</h4>
              {calibration.length ? (
                <ResponsiveContainer width="100%" height={140}>
                  <ComposedChart data={calibration} margin={{ top: 4, right: 8, bottom: 0, left: -22 }}>
                    <XAxis dataKey="decile" tick={{ fontSize: 9 }} />
                    <YAxis domain={[0, 1]} tick={{ fontSize: 9 }} />
                    <Tooltip contentStyle={TOOLTIP} formatter={tooltipFormatter(3)} />
                    <Bar dataKey="predicted" fill="var(--accent-soft, #c7d2fe)" radius={[2, 2, 0, 0]} />
                    <Line dataKey="observed" stroke="var(--accent, #4f46e5)" strokeWidth={2} dot={{ r: 2.5 }} />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <div className="muted" style={{ fontSize: 11 }}>
                  {why(calSummary) ?? 'no calibration decile persisted'}
                </div>
              )}
              <div className="muted" style={{ fontSize: 10.5 }}>
                d1 = the lowest predicted survival · bars: mean predicted S(H), dots: KM-observed ·
                ICI {fmtNum(num(calSummary, 'ici'), 3)} · E50 {fmtNum(num(calSummary, 'e50'), 3)}
              </div>
            </section>
          </>
        )
      }}
    </AsyncPanel>
  )
}
