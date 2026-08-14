import { type ReactNode, useState } from 'react'
import { NavLink, Link } from 'react-router-dom'
import { useIdentity } from '../identity'
import { signOut } from '../auth'
import { useTheme } from '../theme'
import { Icon } from './ui'

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (!parts.length) return '?'
  return (parts[0][0] + (parts[1]?.[0] ?? '')).toUpperCase()
}

function getCollapsed(): boolean {
  try {
    return localStorage.getItem('sap-sidebar') === 'collapsed'
  } catch {
    return false
  }
}

export default function Shell({ children }: { children: ReactNode }) {
  const { identity } = useIdentity()
  const { theme, toggle } = useTheme()
  const [collapsed, setCollapsed] = useState<boolean>(getCollapsed)
  const user = identity?.user

  const linkClass = ({ isActive }: { isActive: boolean }) => 'sidelink' + (isActive ? ' active' : '')

  const toggleSidebar = () =>
    setCollapsed((c) => {
      const next = !c
      try {
        localStorage.setItem('sap-sidebar', next ? 'collapsed' : 'expanded')
      } catch {
        /* ignore */
      }
      return next
    })

  return (
    <div className="app">
      <header className="appbar">
        <button
          className="icon-btn"
          onClick={toggleSidebar}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-label="Toggle sidebar"
        >
          <Icon name="menu" size={18} />
        </button>
        <Link to="/agents" className="brand" title="AI Apps">
          <span className="brand-mark">
            <Icon name="zap" size={18} />
          </span>
          <span className="brand-name">AI Apps</span>
        </Link>

        <div className="appbar-spacer" />

        <Link to="/create-agent" className="btn primary sm">
          <Icon name="plus" size={14} /> New AI App
        </Link>
        <button
          className="icon-btn"
          onClick={toggle}
          title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          aria-label="Toggle color theme"
        >
          {theme === 'dark' ? <Icon name="sun" size={17} /> : <Icon name="moon" size={17} />}
        </button>
        <div className="appbar-user" title={user ? `${user.name} · ${user.username}` : 'Not signed in'}>
          {user ? initials(user.name || user.username) : '…'}
        </div>
        <button className="btn ghost sm" onClick={() => void signOut()} title="Sign out">
          Sign out
        </button>
      </header>

      <div className="body">
        <aside className={'sidebar' + (collapsed ? ' collapsed' : '')}>
          <nav className="sidenav">
            <div className="group-label">Build</div>
            <NavLink className={linkClass} to="/agents" title="AI Apps">
              <span className="ico">
                <Icon name="grid" size={17} />
              </span>
              <span className="label">AI Apps</span>
            </NavLink>
            <NavLink className={linkClass} to="/playground" title="Playground">
              <span className="ico">
                <Icon name="message" size={17} />
              </span>
              <span className="label">Playground</span>
            </NavLink>
          </nav>
          <button
            className="collapse-toggle"
            onClick={toggleSidebar}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            <span className="chev">{collapsed ? '»' : '«'}</span>
            <span className="label">Collapse</span>
          </button>
        </aside>

        <main className="main">{children}</main>
      </div>
    </div>
  )
}
