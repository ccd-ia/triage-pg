/*
 * ModelGroupsTable — the sortable model-groups table (Model Groups sub-tab),
 * from /experiments/{hash}/model-groups. A row click opens the group's
 * best/representative model in the ModelSheet. Columns: group, algorithm,
 * hyperparameters, #features, #models, train-end span — and, when the page
 * passes the audition ranking (plan P6), avg ± σ + max regret for the
 * effective metric, so the table stops being metric-blind.
 */
import { useMemo, useState } from 'react'
import type { ExpAuditionRankRow, ModelGroupSummaryRow } from '../api/types'
import { abbrevAlgo } from '../api/transforms'

type SortKey = 'model_group_id' | 'model_type' | 'n_models' | 'last_train_end' | 'avg'

const shortType = abbrevAlgo

function hyperText(h: Record<string, unknown> | null): string {
  if (!h) return '—'
  return Object.entries(h)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(' · ')
}

interface Props {
  groups: ModelGroupSummaryRow[]
  selectedGroupId: number | null
  /** Resolve a group → a model_id (the page provides via leaderboard/evals). */
  onPickGroup: (group: ModelGroupSummaryRow) => void
  /** Open the group's own detail panel (vs. a row click, which opens its model). */
  onOpenGroupPanel?: (group: ModelGroupSummaryRow) => void
  /** Audition ranking rows at the effective metric (adds avg ± σ / regret columns). */
  ranking?: ExpAuditionRankRow[]
  /** The metric the ranking columns describe (header label). */
  metricLabel?: string
}

export function ModelGroupsTable({
  groups,
  selectedGroupId,
  onPickGroup,
  onOpenGroupPanel,
  ranking,
  metricLabel,
}: Props) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'model_group_id', dir: 1 })
  const aggOf = useMemo(() => {
    const map = new Map<number, ExpAuditionRankRow>()
    for (const r of ranking ?? []) map.set(r.model_group_id, r)
    return map
  }, [ranking])
  const hasAgg = aggOf.size > 0

  const sorted = useMemo(() => {
    const arr = [...groups]
    arr.sort((a, b) => {
      const av =
        sort.key === 'avg' ? (aggOf.get(a.model_group_id)?.avg_value ?? null) : a[sort.key]
      const bv =
        sort.key === 'avg' ? (aggOf.get(b.model_group_id)?.avg_value ?? null) : b[sort.key]
      if (av == null) return 1
      if (bv == null) return -1
      if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sort.dir
      return String(av).localeCompare(String(bv)) * sort.dir
    })
    return arr
  }, [groups, sort, aggOf])

  const toggle = (key: SortKey) =>
    setSort((prev) => (prev.key === key ? { key, dir: (prev.dir * -1) as 1 | -1 } : { key, dir: 1 }))

  const arrow = (key: SortKey) => (sort.key === key ? (sort.dir === 1 ? ' ▲' : ' ▼') : '')

  return (
    <table>
      <thead>
        <tr>
          <th className="clickrow" onClick={() => toggle('model_group_id')}>
            group{arrow('model_group_id')}
          </th>
          <th className="clickrow" onClick={() => toggle('model_type')}>
            algorithm{arrow('model_type')}
          </th>
          <th>hyperparameters</th>
          <th className="num">features</th>
          <th className="num clickrow" onClick={() => toggle('n_models')}>
            models{arrow('n_models')}
          </th>
          <th className="clickrow" onClick={() => toggle('last_train_end')}>
            train-end span{arrow('last_train_end')}
          </th>
          {hasAgg ? (
            <>
              <th className="num clickrow" onClick={() => toggle('avg')}>
                {metricLabel ?? 'metric'} avg ± σ{arrow('avg')}
              </th>
              <th className="num">max regret</th>
            </>
          ) : null}
          {onOpenGroupPanel ? <th /> : null}
        </tr>
      </thead>
      <tbody>
        {sorted.map((g) => (
          <tr
            key={g.model_group_id}
            className="clickrow"
            onClick={() => onPickGroup(g)}
            style={g.model_group_id === selectedGroupId ? { background: 'var(--acc-bg)' } : undefined}
          >
            <td className="mono">g{g.model_group_id}</td>
            <td>{shortType(g.model_type)}</td>
            <td className="mono muted">{hyperText(g.hyperparameters)}</td>
            <td className="num">{g.feature_list?.length ?? '—'}</td>
            <td className="num">{g.n_models}</td>
            <td className="mono">
              {g.first_train_end?.slice(0, 7) ?? '—'} → {g.last_train_end?.slice(0, 7) ?? '—'}
            </td>
            {hasAgg ? (
              <>
                <td className="num">
                  {aggOf.get(g.model_group_id)?.avg_value?.toFixed(3) ?? '—'} ±{' '}
                  {aggOf.get(g.model_group_id)?.stddev_value?.toFixed(3) ?? '—'}
                </td>
                <td className="num">
                  {aggOf.get(g.model_group_id)?.max_regret?.toFixed(3) ?? '—'}
                </td>
              </>
            ) : null}
            {onOpenGroupPanel ? (
              <td>
                <button
                  type="button"
                  className="seg"
                  onClick={(e) => {
                    e.stopPropagation()
                    onOpenGroupPanel(g)
                  }}
                  title="open this group's panel"
                >
                  ▤ group
                </button>
              </td>
            ) : null}
          </tr>
        ))}
      </tbody>
    </table>
  )
}
