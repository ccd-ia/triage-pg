/*
 * Shared DAG layout for the derivation/pipeline graphs (xyflow).
 *
 * The hand-rolled longest-path layout stacked every same-rank node in one column
 * and let fitView shrink the labels to mush (the "barely readable" derivation
 * graph). Dagre gives a proper layered left→right layout with real rank/node
 * separation, and `collapseKind` folds a wide fan-out (e.g. 90+ model leaves) into
 * one "kind ×N" node so the graph stays legible.
 */
import dagre from '@dagrejs/dagre'
import { Position, type Edge, type Node } from '@xyflow/react'

export interface GraphNode {
  id: string
  kind: string
  label: string
  className: string
}

export interface GraphEdge {
  source: string
  target: string
}

export const NODE_W = 160
export const NODE_H = 44

/** Run dagre (LR) and return positioned xyflow nodes + edges. */
export function layoutDag(
  nodes: GraphNode[],
  edges: GraphEdge[],
): { nodes: Node[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'LR', nodesep: 22, ranksep: 80, marginx: 16, marginy: 16 })
  g.setDefaultEdgeLabel(() => ({}))
  const ids = new Set(nodes.map((n) => n.id))
  for (const n of nodes) g.setNode(n.id, { width: NODE_W, height: NODE_H })
  for (const e of edges) {
    if (ids.has(e.source) && ids.has(e.target)) g.setEdge(e.source, e.target)
  }
  dagre.layout(g)

  const xy: Node[] = nodes.map((n) => {
    const p = g.node(n.id)
    return {
      id: n.id,
      position: { x: (p?.x ?? 0) - NODE_W / 2, y: (p?.y ?? 0) - NODE_H / 2 },
      data: { label: n.label },
      className: n.className,
      draggable: false,
      connectable: false,
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      style: { whiteSpace: 'pre-line' as const, width: NODE_W },
    }
  })
  const es: Edge[] = edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({
      id: `${e.source}->${e.target}`,
      source: e.source,
      target: e.target,
      style: { stroke: 'var(--line)' },
    }))
  return { nodes: xy, edges: es }
}

/**
 * Collapse every node of `kind` into ONE synthetic node `__collapsed_<kind>`, rewiring
 * edges that touch a collapsed node to the synthetic node (deduped, no self-loops). The
 * synthetic node inherits the most common className among the collapsed nodes so its
 * status colour is representative. Returns the collapsed count (0 = nothing collapsed).
 */
export function collapseKind(
  nodes: GraphNode[],
  edges: GraphEdge[],
  kind: string,
): { nodes: GraphNode[]; edges: GraphEdge[]; collapsed: number } {
  const victims = nodes.filter((n) => n.kind === kind)
  if (victims.length < 2) return { nodes, edges, collapsed: 0 }

  const synthId = `__collapsed_${kind}`
  const victimIds = new Set(victims.map((n) => n.id))
  // Most common className among collapsed nodes (representative status colour).
  const counts = new Map<string, number>()
  for (const v of victims) counts.set(v.className, (counts.get(v.className) ?? 0) + 1)
  const className = [...counts.entries()].sort((a, b) => b[1] - a[1])[0][0]

  const kept = nodes.filter((n) => n.kind !== kind)
  kept.push({ id: synthId, kind, label: `${kind} ×${victims.length}`, className })

  const remap = (id: string) => (victimIds.has(id) ? synthId : id)
  const seen = new Set<string>()
  const newEdges: GraphEdge[] = []
  for (const e of edges) {
    const s = remap(e.source)
    const t = remap(e.target)
    if (s === t) continue // drop self-loops created by collapsing
    const key = `${s}->${t}`
    if (seen.has(key)) continue
    seen.add(key)
    newEdges.push({ source: s, target: t })
  }
  return { nodes: kept, edges: newEdges, collapsed: victims.length }
}
