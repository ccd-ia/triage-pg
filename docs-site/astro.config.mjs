// The triage-pg docs site (plan: specs/dssg-style-tutorials-github-pages.html).
// Project-pages deployment: every internal URL must respect base '/triage-pg'.
import { defineConfig } from 'astro/config'
import starlight from '@astrojs/starlight'
import starlightLinksValidator from 'starlight-links-validator'
import mermaid from 'astro-mermaid'

export default defineConfig({
  site: 'https://ccd-ia.github.io',
  base: '/triage-pg',
  integrations: [
    // ```mermaid fences render client-side, theme-synced with the site (light/dark).
    // Must precede starlight() so it processes the markdown first.
    mermaid({ autoTheme: true }),
    starlight({
      title: 'triage-pg',
      description:
        'PostgreSQL-native, deliberately simplified fork of DSSG triage for temporal ML on tabular public-policy data.',
      customCss: ['./src/styles/house.css'],
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/ccd-ia/triage-pg' },
      ],
      // i18n: English lives at the root (published URLs unchanged); Spanish is a
      // full translation under /es/ (content in src/content/docs/es/**, mirroring
      // the tree). Starlight ships its own es UI strings; the language picker
      // appears once a second locale exists.
      defaultLocale: 'root',
      locales: {
        root: { label: 'English', lang: 'en' },
        es: { label: 'Español', lang: 'es' },
      },
      sidebar: [
        {
          label: 'Start here',
          translations: { es: 'Empieza aquí' },
          items: [
            { label: 'Welcome', translations: { es: 'Bienvenida' }, slug: 'index' },
            { label: 'FAQ', translations: { es: 'Preguntas frecuentes' }, slug: 'faq' },
            // The two published artifacts are static passthrough pages (public/),
            // linked absolutely so the links-validator does not treat them as
            // routes. They exist in English only.
            { label: 'Onboarding one-pager', link: 'https://ccd-ia.github.io/triage-pg/onboarding.html' },
            { label: 'vs DSSG triage', link: 'https://ccd-ia.github.io/triage-pg/triage-pg-vs-dssg-triage.html' },
          ],
        },
        {
          label: 'Concepts',
          translations: { es: 'Conceptos' },
          autogenerate: { directory: 'concepts' },
        },
        {
          label: 'Tutorials',
          translations: { es: 'Tutoriales' },
          autogenerate: { directory: 'tutorials' },
        },
        {
          label: 'Reference',
          translations: { es: 'Referencia' },
          autogenerate: { directory: 'reference' },
        },
      ],
      plugins: [starlightLinksValidator()],
    }),
  ],
})
