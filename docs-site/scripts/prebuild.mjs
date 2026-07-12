/*
 * Passthrough prebuild: the two self-contained HTML artifacts and the doc images
 * live in ../docs (the repo's committed docs tree). Copying them into public/
 * keeps their published URLs stable across the IA switch (the Starlight index
 * replaced onboarding.html at the site root, but README + the v1.0.0-rc1
 * Release assets still deep-link /triage-pg/onboarding.html etc.).
 * public/ copies are generated — gitignored, never edited.
 */
import { cpSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const docs = join(here, '..', '..', 'docs')
const pub = join(here, '..', 'public')

mkdirSync(pub, { recursive: true })
for (const f of ['onboarding.html', 'triage-pg-vs-dssg-triage.html']) {
  cpSync(join(docs, f), join(pub, f))
}
cpSync(join(docs, 'images'), join(pub, 'images'), { recursive: true })
console.log('passthrough: onboarding.html, triage-pg-vs-dssg-triage.html, images/')
