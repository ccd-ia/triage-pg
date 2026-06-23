/*
 * ModelGroupsTable — the sortable model-groups table (Model Groups sub-tab),
 * from /experiments/{hash}/model-groups. A row click opens the group's
 * best/representative model in the ModelSheet. Columns: group, algorithm,
 * hyperparameters, #features, #models, train-end span.
 */
import { useMemo, useState } from 'react'
import type { ModelGroupSummaryRow } from '../api/types'

type SortKey = 'model_group_id' | 'model_type' | 'n_models' | 'last_train_end'

function shortType(t: string | null): string {
  if (!t) return '—'
  return (t.split('.').pop() ?? t).replace(/Classifier$|Regressor$/, '')
}

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
}

export function ModelGroupsTable({ groups, selectedGroupId, onPickGroup }: Props) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'model_group_id', dir: 1 })

  const sorted = useMemo(() => {
    const arr = [...groups]
    arr.sort((a, b) => {
      const av = a[sort.key]
      const bv = b[sort.key]
      if (av == null) return 1
      if (bv == null) return -1
      if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sort.dir
      return String(av).localeCompare(String(bv)) * sort.dir
    })
    return arr
  }, [groups, sort])

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
          </tr>
        ))}
      </tbody>
    </table>
  )
}
