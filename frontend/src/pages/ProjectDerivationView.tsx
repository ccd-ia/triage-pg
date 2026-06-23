/*
 * ProjectDerivationView (/derivation) — the project-wide derivation graph across
 * ALL experiments (/derivation). Nodes shared across experiments
 * (n_experiments > 1) are highlighted (cache-shared artifacts — the Guix-style
 * identity that makes experiment-scoped analysis correct). Reuses the layered
 * xyflow layout from the run-scoped DerivationGraph.
 */
import { useMemo } from 'react'
import { ReactFlow, Background, Position, type Edge, type Node } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { ProjectDerivationNode } from '../api/types'

const LAYER_X = 210
const ROW_Y = 64

function nodeClass(n: ProjectDerivationNode): string {
  if (n.n_experiments > 1) return 'cachehit' // shared across experiments → highlight
  switch (n.status) {
    case 'built':
      return 'built'
    case 'building':
      return 'building'
    case 'collected':
      return 'collected'
    default:
      return 'todo'
  }
}

function nodeLabel(n: ProjectDerivationNode): string {
  const shared = n.n_experiments > 1 ? ` ⇄ ${n.n_experiments} exp` : ''
  const id = n.artifact_id.length > 12 ? n.artifact_id.slice(0, 12) : n.artifact_id
  return `${n.kind}\n${id}${shared}`
}

function layerOf(
  id: string,
  parents: Map<string, string[]>,
  memo: Map<string, number>,
  guard: Set<string>,
): number {
  if (memo.has(id)) return memo.get(id)!
  if (guard.has(id)) return 0
  guard.add(id)
  const ps = parents.get(id) ?? []
  const layer = ps.length === 0 ? 0 : 1 + Math.max(...ps.map((p) => layerOf(p, parents, memo, guard)))
  guard.delete(id)
  memo.set(id, layer)
  return layer
}

export function ProjectDerivationView() {
  const deriv = useAsync(() => api.projectDerivation(), [])

  const flow = useMemo(() => {
    if (!deriv.data) return { nodes: [] as Node[], edges: [] as Edge[] }
    const parents = new Map<string, string[]>()
    for (const e of deriv.data.edges) {
      const arr = parents.get(e.artifact_id) ?? []
      arr.push(e.parent_id)
      parents.set(e.artifact_id, arr)
    }
    const memo = new Map<string, number>()
    const layers = new Map<string, number>()
    for (const n of deriv.data.nodes) layers.set(n.artifact_id, layerOf(n.artifact_id, parents, memo, new Set()))

    const rowCursor = new Map<number, number>()
    const nodes: Node[] = deriv.data.nodes.map((n) => {
      const layer = layers.get(n.artifact_id) ?? 0
      const row = rowCursor.get(layer) ?? 0
      rowCursor.set(layer, row + 1)
      return {
        id: n.artifact_id,
        position: { x: layer * LAYER_X, y: row * ROW_Y },
        data: { label: nodeLabel(n) },
        className: `flownode ${nodeClass(n)}`,
        draggable: false,
        connectable: false,
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        style: { whiteSpace: 'pre-line' as const },
      }
    })
    const edges: Edge[] = deriv.data.edges.map((e) => ({
      id: `${e.parent_id}->${e.artifact_id}`,
      source: e.parent_id,
      target: e.artifact_id,
      style: { stroke: 'var(--line)' },
    }))
    return { nodes, edges }
  }, [deriv.data])

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
          <div className="flowwrap" style={{ height: 460 }}>
            <ReactFlow
              nodes={flow.nodes}
              edges={flow.edges}
              fitView
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={false}
              proOptions={{ hideAttribution: true }}
            >
              <Background color="var(--line2)" gap={18} />
            </ReactFlow>
          </div>
          <div className="glegend">
            <span>
              <i style={{ background: '#3fb950' }} />
              built
            </span>
            <span>
              <i style={{ background: '#d29922' }} />
              building
            </span>
            <span>
              <i style={{ background: '#bc8cff' }} />
              shared across experiments
            </span>
            <span className="muted">cache-share identity · ADR-0013–0017</span>
          </div>
        </div>
      )}
    </main>
  )
}
