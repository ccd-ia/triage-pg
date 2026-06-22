/*
 * RunMonitor — the tabbed run monitor (spec §1): one panel, four tabs
 * (Pipeline · Derivation · Audition · Bias). Each tab is an independent read;
 * the parent passes the loaded data + a `live` flag for the pulsing dots.
 * Pipeline always re-fetches on a delta; audition/bias re-fetch on
 * kind ∈ {model, evaluation} (wired in RunDetail).
 */
import { useState } from 'react'
import type {
  AuditionResponse,
  BiasResponse,
  DerivationResponse,
  ProgressResponse,
} from '../api/types'
import { PipelineGraph } from './PipelineGraph'
import { DerivationGraph } from './DerivationGraph'
import { AuditionTab } from './AuditionTab'
import { BiasTab } from './BiasTab'

type TabId = 'pipe' | 'deriv' | 'aud' | 'bias'

interface Props {
  progress: ProgressResponse | undefined
  derivation: DerivationResponse | undefined
  audition: AuditionResponse | undefined
  bias: BiasResponse | undefined
  selectedModelLabel: string
  live: boolean
}

export function RunMonitor({
  progress,
  derivation,
  audition,
  bias,
  selectedModelLabel,
  live,
}: Props) {
  const [tab, setTab] = useState<TabId>('pipe')

  return (
    <section className="panel monitor">
      <div className="tabbar">
        <Tab id="pipe" active={tab} onClick={setTab}>
          Pipeline progress
        </Tab>
        <Tab id="deriv" active={tab} onClick={setTab}>
          Derivation graph
        </Tab>
        <Tab id="aud" active={tab} onClick={setTab}>
          Audition {live ? <span className="dotlive" title="live/provisional" /> : null}
        </Tab>
        <Tab id="bias" active={tab} onClick={setTab}>
          Bias {live ? <span className="dotlive" title="live" /> : null}
        </Tab>
      </div>

      {tab === 'pipe' &&
        (progress ? <PipelineGraph data={progress} /> : <Loading what="pipeline" />)}
      {tab === 'deriv' &&
        (derivation ? <DerivationGraph data={derivation} /> : <Loading what="derivation graph" />)}
      {tab === 'aud' &&
        (audition ? <AuditionTab data={audition} /> : <Loading what="audition" />)}
      {tab === 'bias' &&
        (bias ? (
          <BiasTab data={bias} modelLabel={selectedModelLabel} />
        ) : (
          <Loading what="bias metrics" />
        ))}
    </section>
  )
}

function Tab({
  id,
  active,
  onClick,
  children,
}: {
  id: TabId
  active: TabId
  onClick: (id: TabId) => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      className={`tabbtn${active === id ? ' active' : ''}`}
      onClick={() => onClick(id)}
    >
      {children}
    </button>
  )
}

function Loading({ what }: { what: string }) {
  return <div className="muted" style={{ padding: '14px 4px' }}>Loading {what}…</div>
}
