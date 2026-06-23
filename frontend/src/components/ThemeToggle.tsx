/*
 * ThemeToggle (Q11) — flips the document [data-theme] between dark/light.
 * Initial theme: a manual override in localStorage wins; otherwise follow the
 * OS via matchMedia('(prefers-color-scheme: dark)'). The choice is persisted so
 * a manual flip survives reloads, but if the user never flipped it, the bar
 * keeps tracking the OS.
 */
import { useEffect, useState } from 'react'

type Theme = 'dark' | 'light'

const KEY = 'triage-theme'

function osTheme(): Theme {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function initialTheme(): Theme {
  const saved = localStorage.getItem(KEY)
  if (saved === 'dark' || saved === 'light') return saved
  return osTheme()
}

function applyTheme(t: Theme) {
  document.documentElement.setAttribute('data-theme', t)
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(initialTheme)

  // Reflect the current theme onto the document on mount + whenever it changes.
  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  // Follow the OS while the user hasn't set a manual override.
  useEffect(() => {
    if (localStorage.getItem(KEY)) return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => setTheme(mq.matches ? 'dark' : 'light')
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  const toggle = () => {
    setTheme((prev) => {
      const next = prev === 'dark' ? 'light' : 'dark'
      localStorage.setItem(KEY, next)
      return next
    })
  }

  return (
    <button type="button" className="themetoggle" onClick={toggle} title="Toggle light/dark">
      {theme === 'dark' ? '☾ dark' : '☀ light'}
    </button>
  )
}
