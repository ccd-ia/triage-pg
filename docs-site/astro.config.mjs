// The triage-pg docs site (plan: specs/dssg-style-tutorials-github-pages.html).
// Project-pages deployment: every internal URL must respect base '/triage-pg'.
import { defineConfig } from 'astro/config'
import starlight from '@astrojs/starlight'
import starlightLinksValidator from 'starlight-links-validator'

export default defineConfig({
  site: 'https://ccd-ia.github.io',
  base: '/triage-pg',
  integrations: [
    starlight({
      title: 'triage-pg',
      description:
        'PostgreSQL-native, deliberately simplified fork of DSSG triage for temporal ML on tabular public-policy data.',
      customCss: ['./src/styles/house.css'],
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/ccd-ia/triage-pg' },
      ],
      sidebar: [
        {
          label: 'Start here',
          items: [
            { label: 'Welcome', slug: 'index' },
            // The two published artifacts are static passthrough pages (public/),
            // linked absolutely so the links-validator does not treat them as routes.
            { label: 'Onboarding one-pager', link: 'https://ccd-ia.github.io/triage-pg/onboarding.html' },
            { label: 'vs DSSG triage', link: 'https://ccd-ia.github.io/triage-pg/triage-pg-vs-dssg-triage.html' },
          ],
        },
        {
          label: 'Tutorials',
          autogenerate: { directory: 'tutorials' },
        },
        {
          label: 'Reference',
          autogenerate: { directory: 'reference' },
        },
      ],
      plugins: [starlightLinksValidator()],
    }),
  ],
})
