/*
 * FairnessTreeWizard — the Aequitas Fairness Tree as a guidance layer (plan P2).
 *
 * Two questions route attention to the disparity metric that matters for THIS
 * intervention (credit: the Aequitas fairness tree,
 * https://datasciencepublicpolicy.org/our-work/tools-guides/aequitas/):
 *
 *   intervention type?      punitive          assistive        representation
 *   act on a capped top-k?  yes -> FDR        yes -> FNR       (either) -> selection rate
 *                           no  -> FPR        no  -> FOR
 *
 * It highlights and explains — it NEVER hides the other metrics or blocks anything;
 * fairness judgment stays with the human (docs/fairness.md). Preseeded from the
 * experiment's bias_config.intervention when present.
 */
import { useEffect, useState } from 'react'
import {
  fairnessFocus,
  type FairnessFocus,
  type Intervention,
} from '../api/transforms'

export function FairnessTreeWizard({
  intervention: seeded,
  onFocus,
}: {
  /** preseed from bias_config.intervention (config-driven default). */
  intervention?: Intervention | null
  onFocus: (focus: FairnessFocus) => void
}) {
  const [intervention, setIntervention] = useState<Intervention>(seeded ?? 'punitive')
  const [capped, setCapped] = useState(true)

  useEffect(() => {
    onFocus(fairnessFocus(intervention, capped))
    // onFocus is a stable setter from the tab; re-run only on answer changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervention, capped])

  const focus = fairnessFocus(intervention, capped)
  return (
    <div className="card" style={{ padding: '10px 12px', marginBottom: 12 }}>
      <div style={{ display: 'flex', gap: 14, alignItems: 'center', flexWrap: 'wrap' }}>
        <b style={{ fontSize: 12 }}>Fairness tree</b>
        <label className="field" style={{ margin: 0 }}>
          <span>the intervention is</span>
          <select
            value={intervention}
            onChange={(e) => setIntervention(e.target.value as Intervention)}
          >
            <option value="punitive">punitive (a flag causes harm)</option>
            <option value="assistive">assistive (a miss causes harm)</option>
            <option value="representation">about representation</option>
          </select>
        </label>
        {intervention !== 'representation' ? (
          <label className="field" style={{ margin: 0 }}>
            <span>acting on</span>
            <select
              value={capped ? 'capped' : 'broad'}
              onChange={(e) => setCapped(e.target.value === 'capped')}
            >
              <option value="capped">a capped top-k list</option>
              <option value="broad">everyone flagged</option>
            </select>
          </label>
        ) : null}
        <span className="badge b-aud">focus: {focus.primary.toUpperCase()}</span>
        {seeded ? <span className="muted" style={{ fontSize: 10 }}>preset by bias_config</span> : null}
      </div>
      <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
        {focus.rationale} All metrics stay visible — the tree routes attention, it does not
        decide. Credit: the{' '}
        <a
          href="https://datasciencepublicpolicy.org/our-work/tools-guides/aequitas/"
          target="_blank"
          rel="noreferrer"
        >
          Aequitas fairness tree
        </a>{' '}
        · see <span className="mono">docs/fairness.md</span>.
      </div>
    </div>
  )
}
