/*
 * DerivationGraph — the Guix-style derivation closure (spec §1 tab 2, §3.6):
 * nodes = triage.artifacts in the run closure, edges = triage.artifact_inputs,
 * colored by status (built/building/collected) with cache-hits shaded. Provides
 * provenance / reproducibility (ADRs 0013–0017).
 *
 * The {nodes, edges} contract is a real DAG, so we compute a simple layered
 * layout (longest-path from sources) for @xyflow/react instead of a chain.
 */
import { useMemo } from 'react'
import { ReactFlow, Background, Position, type Edge, type Node } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { DerivationNode, DerivationResponse } from '../api/types'

const LAYER_X = 200
const ROW_Y = 56

function nodeClass(n: DerivationNode): string {
  if (n.cache_hit) return 'cachehit'
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

/**
 * A node label derived from the raw artifact (routes.py returns no label field):
 * the kind, a short artifact id, plus a marker for cache-hit / status.
 */
function nodeLabel(n: DerivationNode): string {
  const shortId = n.artifact_id.length > 10 ? n.artifact_id.slice(0, 10) : n.artifact_id
  const marker = n.cache_hit ? ' ⟲ cache' : n.status === 'building' ? ' ◐' : ''
  return `${n.kind}\n${shortId}${marker}`
}

/** Longest-path layering: a node's layer = 1 + max(parent layers). */
function layerOf(
  id: string,
  parents: Map<string, string[]>,
  memo: Map<string, number>,
  guard: Set<string>,
): number {
  if (memo.has(id)) return memo.get(id)!
  if (guard.has(id)) return 0 // cycle guard (closures are acyclic, defensive)
  guard.add(id)
  const ps = parents.get(id) ?? []
  const layer = ps.length === 0 ? 0 : 1 + Math.max(...ps.map((p) => layerOf(p, parents, memo, guard)))
  guard.delete(id)
  memo.set(id, layer)
  return layer
}

export function DerivationGraph({ data }: { data: DerivationResponse }) {
  const { nodes, edges } = useMemo(() => {
    const parents = new Map<string, string[]>()
    for (const e of data.edges) {
      const arr = parents.get(e.artifact_id) ?? []
      arr.push(e.parent_id)
      parents.set(e.artifact_id, arr)
    }

    const memo = new Map<string, number>()
    const layers = new Map<string, number>()
    for (const n of data.nodes) {
      layers.set(n.artifact_id, layerOf(n.artifact_id, parents, memo, new Set()))
    }

    // Assign a row within each layer.
    const rowCursor = new Map<number, number>()
    const ns: Node[] = data.nodes.map((n) => {
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

    const es: Edge[] = data.edges.map((e) => ({
      id: `${e.parent_id}->${e.artifact_id}`,
      source: e.parent_id,
      target: e.artifact_id,
      style: { stroke: 'var(--line)' },
    }))
    return { nodes: ns, edges: es }
  }, [data])

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
        <span className="src">artifacts ⋈ artifact_inputs (run closure)</span>
      </div>
      <div className="flowwrap" style={{ height: 260 }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
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
          <i style={{ background: '#56607a' }} />
          collected (GC)
        </span>
        <span>
          <i style={{ background: '#bc8cff' }} />
          cache hit
        </span>
        <span className="muted">provenance / reproducibility · ADR-0013–0017</span>
      </div>
    </>
  )
}
