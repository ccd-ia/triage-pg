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
  /** Swimlane key (the temporal split / train_end date); null/undefined = the shared lane. */
  lane?: string | null
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

const SHARED_LANE = '∑ shared'
// Pipeline depth per artifact kind — the column a node sits in within its swimlane.
const COL_BY_KIND: Record<string, number> = {
  source: 0,
  cohort: 1,
  labels: 1,
  feature_group: 1,
  matrix: 2,
  model: 3,
  evaluate: 4,
}
const COL_X = 200
const SWIM_NODE_H = 46
const ROW_GAP = 10
const LANE_GAP = 26
const LANE_LABEL_W = 96

function laneOf(n: GraphNode): string {
  return n.lane ?? SHARED_LANE
}

/**
 * Collapse model nodes PER LANE (per temporal split) into one "model ×N" node, so each split
 * lane stays legible while still showing its own model fan-out. Returns the collapsed count.
 */
export function collapseModelsByLane(
  nodes: GraphNode[],
  edges: GraphEdge[],
): { nodes: GraphNode[]; edges: GraphEdge[]; collapsed: number } {
  const models = nodes.filter((n) => n.kind === 'model')
  if (models.length < 2) return { nodes, edges, collapsed: 0 }

  const byLane = new Map<string, GraphNode[]>()
  for (const m of models) {
    const lane = laneOf(m)
    const arr = byLane.get(lane) ?? []
    arr.push(m)
    byLane.set(lane, arr)
  }
  const kept = nodes.filter((n) => n.kind !== 'model')
  const victimLane = new Map<string, string>()
  const synthByLane = new Map<string, string>()
  let collapsed = 0
  for (const [lane, ms] of byLane) {
    if (ms.length < 2) {
      kept.push(...ms)
      continue
    }
    collapsed += ms.length
    const synthId = `__models_${lane}`
    synthByLane.set(lane, synthId)
    for (const m of ms) victimLane.set(m.id, lane)
    kept.push({
      id: synthId,
      kind: 'model',
      label: `model ×${ms.length}`,
      className: ms[0].className,
      lane: ms[0].lane,
    })
  }
  if (!collapsed) return { nodes, edges, collapsed: 0 }

  const remap = (id: string) =>
    victimLane.has(id) ? synthByLane.get(victimLane.get(id)!)! : id
  const seen = new Set<string>()
  const newEdges: GraphEdge[] = []
  for (const e of edges) {
    const s = remap(e.source)
    const t = remap(e.target)
    if (s === t) continue
    const key = `${s}->${t}`
    if (seen.has(key)) continue
    seen.add(key)
    newEdges.push({ source: s, target: t })
  }
  return { nodes: kept, edges: newEdges, collapsed }
}

/**
 * Swimlane layout: one horizontal lane per temporal split (train cutoff → test period), with a
 * "shared" lane on top for the cohort/labels/feature_group/source nodes that feed every split.
 * Within a lane, nodes sit in columns by pipeline depth (source→cohort→matrix→model). Emits a
 * left-edge label node per lane so the splits read top-to-bottom.
 */
export function layoutSwimlanes(
  gnodes: GraphNode[],
  edges: GraphEdge[],
): { nodes: Node[]; edges: Edge[] } {
  const splitLanes = [...new Set(gnodes.map(laneOf).filter((l) => l !== SHARED_LANE))].sort()
  const lanes = [SHARED_LANE, ...splitLanes]

  // rows per (lane, col) to size each lane's height
  const rowsPerCol = new Map<string, Map<number, number>>()
  for (const n of gnodes) {
    const lane = laneOf(n)
    const col = COL_BY_KIND[n.kind] ?? 1
    const m = rowsPerCol.get(lane) ?? new Map<number, number>()
    m.set(col, (m.get(col) ?? 0) + 1)
    rowsPerCol.set(lane, m)
  }
  const laneY = new Map<string, number>()
  let y = 0
  for (const lane of lanes) {
    const m = rowsPerCol.get(lane) ?? new Map<number, number>()
    const maxRows = Math.max(1, ...[...m.values(), 1])
    laneY.set(lane, y)
    y += maxRows * (SWIM_NODE_H + ROW_GAP) + LANE_GAP
  }

  const cursor = new Map<string, number>() // `${lane}:${col}` -> next row
  const ns: Node[] = []
  for (const lane of lanes) {
    ns.push({
      id: `__lane_${lane}`,
      position: { x: 0, y: laneY.get(lane)! },
      data: { label: lane === SHARED_LANE ? 'shared' : `split\n${lane}` },
      className: 'lanelabel',
      draggable: false,
      connectable: false,
      selectable: false,
      style: { width: LANE_LABEL_W - 14, whiteSpace: 'pre-line' as const },
    })
  }
  for (const n of gnodes) {
    const lane = laneOf(n)
    const col = COL_BY_KIND[n.kind] ?? 1
    const key = `${lane}:${col}`
    const row = cursor.get(key) ?? 0
    cursor.set(key, row + 1)
    ns.push({
      id: n.id,
      position: {
        x: LANE_LABEL_W + col * COL_X,
        y: laneY.get(lane)! + row * (SWIM_NODE_H + ROW_GAP),
      },
      data: { label: n.label },
      className: n.className,
      draggable: false,
      connectable: false,
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      style: { whiteSpace: 'pre-line' as const, width: NODE_W },
    })
  }
  const ids = new Set(gnodes.map((n) => n.id))
  const es: Edge[] = edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({
      id: `${e.source}->${e.target}`,
      source: e.source,
      target: e.target,
      style: { stroke: 'var(--line)' },
    }))
  return { nodes: ns, edges: es }
}
