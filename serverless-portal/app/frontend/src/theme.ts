// Light/dark color theme. Default is dark; the user's choice persists in
// localStorage and is reflected as <html data-theme="…"> so the CSS token
// overrides in styles.css take effect.
import { useCallback, useSyncExternalStore } from 'react'

export type Theme = 'light' | 'dark'

const KEY = 'sap-theme'

function readStoredTheme(): Theme {
  try {
    // Default to dark; only an explicit 'light' choice opts out.
    return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

// The active theme is kept in a tiny external store so both the toggle button
// and the root CoreAIFluentProvider (which needs the light/dark Fluent theme)
// react to changes via useSyncExternalStore.
let currentTheme: Theme = readStoredTheme()
const listeners = new Set<() => void>()

export function getStoredTheme(): Theme {
  return currentTheme
}

export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme)
}

export function storeTheme(theme: Theme): void {
  currentTheme = theme
  try {
    localStorage.setItem(KEY, theme)
  } catch {
    /* ignore quota/availability errors */
  }
  applyTheme(theme)
  for (const listener of listeners) listener()
}

export function subscribeTheme(onChange: () => void): () => void {
  listeners.add(onChange)
  return () => {
    listeners.delete(onChange)
  }
}

// Apply the saved theme as soon as this module loads (before React renders) so
// there's no flash of the wrong theme.
applyTheme(currentTheme)

export function useThemeMode(): Theme {
  return useSyncExternalStore(subscribeTheme, getStoredTheme, getStoredTheme)
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const theme = useThemeMode()
  const toggle = useCallback(() => {
    storeTheme(getStoredTheme() === 'dark' ? 'light' : 'dark')
  }, [])
  return { theme, toggle }
}
