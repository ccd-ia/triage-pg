/*
 * DerivationGraph — the Guix-style derivation closure (ADRs 0013–0017): nodes =
 * triage.artifacts in the run closure, edges = triage.artifact_inputs, coloured by
 * status (built/building/collected) with cache-hits shaded.
 *
 * Layout is dagre (left→right layered) so ranks separate cleanly and labels stay
 * readable; the model fan-out collapses to one "model ×N" node by default (toggle to
 * expand), and xyflow Controls give zoom/fit. (Readability rework.)
 */
import { useMemo, useState } from 'react'
import { ReactFlow, Background, Controls } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { DerivationNode, DerivationResponse } from '../api/types'
import { collapseKind, layoutDag, type GraphNode } from '../api/graphLayout'

function nodeClass(n: DerivationNode): string {
  if (n.cache_hit) return 'flownode cachehit'
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

function nodeLabel(n: DerivationNode): string {
  const shortId = n.artifact_id.length > 10 ? n.artifact_id.slice(0, 10) : n.artifact_id
  const marker = n.cache_hit ? ' ⟲' : n.status === 'building' ? ' ◐' : ''
  return `${n.kind}\n${shortId}${marker}`
}

export function DerivationGraph({ data }: { data: DerivationResponse }) {
  const [collapsed, setCollapsed] = useState(true)

  const { nodes, edges } = useMemo(() => {
    const gnodes: GraphNode[] = data.nodes.map((n) => ({
      id: n.artifact_id,
      kind: n.kind,
      label: nodeLabel(n),
      className: nodeClass(n),
    }))
    const gedges = data.edges.map((e) => ({ source: e.parent_id, target: e.artifact_id }))
    const folded = collapsed ? collapseKind(gnodes, gedges, 'model') : { nodes: gnodes, edges: gedges }
    return layoutDag(folded.nodes, folded.edges)
  }, [data, collapsed])

  const nModels = data.nodes.filter((n) => n.kind === 'model').length

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        {nModels > 1 ? (
          <button type="button" className="seg" onClick={() => setCollapsed((c) => !c)}>
            {collapsed ? `▸ expand ${nModels} models` : '▾ collapse models'}
          </button>
        ) : <span />}
        <span className="src">artifacts ⋈ artifact_inputs (run closure)</span>
      </div>
      <div className="flowwrap" style={{ height: 340 }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
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
        <span><i style={{ background: 'var(--collected-ink)' }} />collected (GC)</span>
        <span><i style={{ background: 'var(--acc2)' }} />cache hit</span>
        <span className="muted">provenance / reproducibility · ADR-0013–0017</span>
      </div>
    </>
  )
}
