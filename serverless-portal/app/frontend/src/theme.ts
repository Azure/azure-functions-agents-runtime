// Light/dark color theme. Default is light; the user's choice persists in
// localStorage and is reflected as <html data-theme="…"> so the CSS token
// overrides in styles.css take effect.
import { useCallback, useState } from 'react'

export type Theme = 'light' | 'dark'

const KEY = 'sap-theme'

export function getStoredTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme)
}

export function storeTheme(theme: Theme): void {
  try {
    localStorage.setItem(KEY, theme)
  } catch {
    /* ignore quota/availability errors */
  }
  applyTheme(theme)
}

// Apply the saved theme as soon as this module loads (before React renders) so
// there's no flash of the wrong theme.
applyTheme(getStoredTheme())

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(getStoredTheme)
  const toggle = useCallback(() => {
    setTheme((prev) => {
      const next: Theme = prev === 'dark' ? 'light' : 'dark'
      storeTheme(next)
      return next
    })
  }, [])
  return { theme, toggle }
}
