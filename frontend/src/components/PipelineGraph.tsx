/*
 * PipelineGraph — the linear pipeline DAG cohort → labels → matrices → models →
 * evaluate, rendered with @xyflow/react. Each node shows the stage name, status
 * (done/current/todo), and N/M counts from runs.plan. Clicking a stage opens a detail
 * panel listing THIS run's artifacts of that stage (id + status, from the run derivation
 * closure) — "info per component, like derivation but scoped to this run".
 * Live: re-fetched on every progress delta by the parent.
 */
import { useMemo, useState } from 'react'
import { ReactFlow, Background, Position, type Edge, type Node } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { DerivationResponse, ProgressResponse, StageKind, StageProgress } from '../api/types'
import { deriveStages } from '../api/transforms'

const STAGE_X = 200
const NODE_Y = 70

/** Stage → the artifact kind(s) it produces, for the per-stage detail list. */
const STAGE_KINDS: Record<StageKind, string[]> = {
  cohort: ['cohort'],
  labels: ['labels'],
  matrices: ['matrix', 'feature_group'],
  models: ['model'],
  evaluate: [],
}

function statusClass(s: StageProgress['status']): string {
  return s === 'done' ? 'built' : s === 'current' ? 'building' : 'todo'
}

function nodeLabel(st: StageProgress): string {
  const count = st.detail ?? (st.m > 1 ? `${st.n} / ${st.m}` : '')
  return count ? `${st.kind}\n${count}` : st.kind
}

export function PipelineGraph({
  data,
  derivation,
}: {
  data: ProgressResponse
  derivation?: DerivationResponse
}) {
  const [selected, setSelected] = useState<StageKind | null>(null)
  const stages = useMemo(() => deriveStages(data), [data])

  const { nodes, edges } = useMemo(() => {
    const ns: Node[] = stages.map((st, i) => ({
      id: st.kind,
      position: { x: i * STAGE_X, y: NODE_Y },
      data: { label: nodeLabel(st) },
      className: `flownode ${statusClass(st.status)}${selected === st.kind ? ' sel' : ''}`,
      draggable: false,
      connectable: false,
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      style: { whiteSpace: 'pre-line' as const, width: 150, cursor: 'pointer' },
    }))
    const es: Edge[] = stages.slice(1).map((st, i) => ({
      id: `${stages[i].kind}->${st.kind}`,
      source: stages[i].kind,
      target: st.kind,
      animated: st.status === 'current',
      style: { stroke: 'var(--line)' },
    }))
    return { nodes: ns, edges: es }
  }, [stages, selected])

  const sel = selected ? stages.find((s) => s.kind === selected) : null
  const selArtifacts = useMemo(() => {
    if (!selected || !derivation) return []
    const kinds = new Set(STAGE_KINDS[selected])
    return derivation.nodes.filter((n) => kinds.has(n.kind))
  }, [selected, derivation])

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
        <span className="src">SSE · artifacts.built_by_run</span>
      </div>
      <div className="flowwrap">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          fitView
          minZoom={0.4}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          onNodeClick={(_, node) => setSelected((cur) => (cur === node.id ? null : (node.id as StageKind)))}
          proOptions={{ hideAttribution: true }}
        >
          <Background color="var(--line2)" gap={18} />
        </ReactFlow>
      </div>

      {sel ? (
        <div className="drill" style={{ marginTop: 10 }}>
          <div className="ph">
            <b>{sel.kind}</b>
            <span className={`badge ${sel.status === 'done' ? 'b-run' : sel.status === 'current' ? 'b-build' : ''}`}>
              {sel.status}
            </span>
          </div>
          <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>
            {sel.n} of {sel.m} built{STAGE_KINDS[sel.kind].length ? ` · this run's ${STAGE_KINDS[sel.kind].join(' / ')} artifacts:` : ''}
          </div>
          {STAGE_KINDS[sel.kind].length === 0 ? (
            <div className="muted" style={{ fontSize: 11 }}>
              evaluations are not artifacts — see the Audition / Model Groups tabs.
            </div>
          ) : selArtifacts.length ? (
            <table>
              <thead>
                <tr>
                  <th>kind</th>
                  <th>artifact</th>
                  <th>status</th>
                </tr>
              </thead>
              <tbody>
                {selArtifacts.map((a) => (
                  <tr key={a.artifact_id}>
                    <td>{a.kind}</td>
                    <td className="mono">{a.artifact_id.slice(0, 16)}</td>
                    <td>
                      <span className={`badge ${a.cache_hit ? 'b-aud' : a.status === 'built' ? 'b-run' : a.status === 'building' ? 'b-build' : ''}`}>
                        {a.cache_hit ? 'cache hit' : a.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="muted" style={{ fontSize: 11 }}>no artifacts of this kind in the run closure yet.</div>
          )}
        </div>
      ) : (
        <div className="glegend">
          <span><i style={{ background: 'var(--ok)' }} />done</span>
          <span><i style={{ background: 'var(--warn)' }} />current</span>
          <span><i style={{ background: 'var(--todo-border)' }} />todo</span>
          <span className="muted">click a stage for its artifacts · N/M from <span className="mono">runs.plan</span></span>
        </div>
      )}
    </>
  )
}
