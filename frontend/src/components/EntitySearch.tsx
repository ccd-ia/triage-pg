/*
 * EntitySearch — jump straight to an entity's profile by id (no need to scroll a
 * predicted list). Numeric exact-match: on submit it opens the EntityDrawer for that
 * entity_id within the current experiment; a missing id surfaces a 404 in the drawer.
 */
import { useState } from 'react'

export function EntitySearch({ onOpen }: { onOpen: (entityId: number) => void }) {
  const [val, setVal] = useState('')

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const id = Number(val.trim())
    if (Number.isInteger(id) && id > 0) onOpen(id)
  }

  const valid = Number.isInteger(Number(val.trim())) && Number(val.trim()) > 0

  return (
    <form className="entitysearch" onSubmit={submit} title="open an entity profile by id">
      <span className="muted" style={{ fontSize: 10.5 }}>entity</span>
      <input
        type="number"
        min={1}
        inputMode="numeric"
        placeholder="id…"
        value={val}
        onChange={(e) => setVal(e.target.value)}
      />
      <button type="submit" className="seg" disabled={!valid}>open ↗</button>
    </form>
  )
}
