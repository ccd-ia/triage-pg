/*
 * PipelineGraph — linear pipeline DAG cohort → labels → matrices → models →
 * evaluate (spec §1 tab 1, §3.3), rendered with @xyflow/react. Each node shows
 * the stage name, status (done/current/todo), and N/M counts from runs.plan.
 * Live: re-fetched on every progress delta by the parent.
 */
import { useMemo } from 'react'
import { ReactFlow, Background, Position, type Edge, type Node } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { ProgressResponse, StageProgress } from '../api/types'

const STAGE_X = 170
const NODE_Y = 60

function statusClass(s: StageProgress['status']): string {
  return s === 'done' ? 'built' : s === 'current' ? 'building' : 'todo'
}

function nodeLabel(st: StageProgress): string {
  const count = st.detail ?? (st.m > 1 ? `${st.n} / ${st.m}` : '')
  return count ? `${st.kind}\n${count}` : st.kind
}

export function PipelineGraph({ data }: { data: ProgressResponse }) {
  const { nodes, edges } = useMemo(() => {
    const ns: Node[] = data.stages.map((st, i) => ({
      id: st.kind,
      position: { x: i * STAGE_X, y: NODE_Y },
      data: { label: nodeLabel(st) },
      className: `flownode ${statusClass(st.status)}`,
      draggable: false,
      connectable: false,
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      style: { whiteSpace: 'pre-line' as const },
    }))
    const es: Edge[] = data.stages.slice(1).map((st, i) => ({
      id: `${data.stages[i].kind}->${st.kind}`,
      source: data.stages[i].kind,
      target: st.kind,
      animated: st.status === 'current',
      style: { stroke: 'var(--line)' },
    }))
    return { nodes: ns, edges: es }
  }, [data])

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
          <i style={{ background: 'var(--ok)' }} />
          done
        </span>
        <span>
          <i style={{ background: 'var(--warn)' }} />
          current
        </span>
        <span>
          <i style={{ background: '#30363d' }} />
          todo
        </span>
        <span className="muted">
          N/M from <span className="mono">runs.plan</span>
        </span>
      </div>
    </>
  )
}
