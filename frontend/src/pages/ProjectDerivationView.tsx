/*
 * ProjectDerivationView (/derivation) — the project-wide derivation graph across ALL
 * experiments. Nodes shared across experiments (n_experiments > 1) are highlighted (the
 * cache-share identity that makes experiment-scoped analysis correct).
 *
 * Layout is dagre (left→right) with the model fan-out collapsed by default — the prior
 * hand-rolled layout stacked ~14 model nodes in one unreadable column (Image #12).
 */
import { useMemo, useState } from 'react'
import { ReactFlow, Background, Controls } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { ProjectDerivationNode } from '../api/types'
import { collapseKind, layoutDag, type GraphNode } from '../api/graphLayout'

function nodeClass(n: ProjectDerivationNode): string {
  if (n.n_experiments > 1) return 'flownode cachehit' // shared across experiments → highlight
  switch (n.status) {
    case 'built':
      return 'flownode built'
    case 'building':
      return 'flownode building'
    case 'collected':
      return 'flownode collected'
    default:
      return 'flownode todo'
  }
}

function nodeLabel(n: ProjectDerivationNode): string {
  const shared = n.n_experiments > 1 ? ` ⇄${n.n_experiments}` : ''
  const id = n.artifact_id.length > 10 ? n.artifact_id.slice(0, 10) : n.artifact_id
  return `${n.kind}\n${id}${shared}`
}

export function ProjectDerivationView() {
  const deriv = useAsync(() => api.projectDerivation(), [])
  const [collapsed, setCollapsed] = useState(true)

  const flow = useMemo(() => {
    if (!deriv.data) return { nodes: [], edges: [] }
    const gnodes: GraphNode[] = deriv.data.nodes.map((n) => ({
      id: n.artifact_id,
      kind: n.kind,
      label: nodeLabel(n),
      className: nodeClass(n),
    }))
    const gedges = deriv.data.edges.map((e) => ({ source: e.parent_id, target: e.artifact_id }))
    const folded = collapsed ? collapseKind(gnodes, gedges, 'model') : { nodes: gnodes, edges: gedges }
    return layoutDag(folded.nodes, folded.edges)
  }, [deriv.data, collapsed])

  const nModels = deriv.data?.nodes.filter((n) => n.kind === 'model').length ?? 0

  return (
    <main className="page">
      <div className="exphead">
        <h2>Project derivation</h2>
        <p className="desc">All artifacts across experiments; shared nodes (used by &gt;1 experiment) are highlighted.</p>
      </div>
      {deriv.loading ? (
        <div className="banner">Loading derivation…</div>
      ) : deriv.error ? (
        <div className="banner err">Failed to load derivation: {deriv.error.message}</div>
      ) : (
        <div className="panel">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            {nModels > 1 ? (
              <button type="button" className="seg" onClick={() => setCollapsed((c) => !c)}>
                {collapsed ? `▸ expand ${nModels} models` : '▾ collapse models'}
              </button>
            ) : <span />}
            <span className="src">artifacts ⋈ artifact_inputs (project)</span>
          </div>
          <div className="flowwrap" style={{ height: 460 }}>
            <ReactFlow
              nodes={flow.nodes}
              edges={flow.edges}
              fitView
              minZoom={0.2}
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={false}
              proOptions={{ hideAttribution: true }}
            >
              <Background color="var(--line2)" gap={18} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
          <div className="glegend">
            <span><i style={{ background: 'var(--ok)' }} />built</span>
            <span><i style={{ background: 'var(--warn)' }} />building</span>
            <span><i style={{ background: 'var(--acc2)' }} />shared across experiments</span>
            <span className="muted">cache-share identity · ADR-0013–0017</span>
          </div>
        </div>
      )}
    </main>
  )
}
