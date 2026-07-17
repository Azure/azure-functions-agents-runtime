import { type ReactNode } from 'react'
import { NavLink } from 'react-router-dom'
import { useIdentity } from '../identity'
import { signOut } from '../auth'

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (!parts.length) return '?'
  return (parts[0][0] + (parts[1]?.[0] ?? '')).toUpperCase()
}

export default function Shell({ children }: { children: ReactNode }) {
  const { identity, subscriptions, selected, setSelected, loading, error } = useIdentity()

  const itemClass = ({ isActive }: { isActive: boolean }) => 'item' + (isActive ? ' active' : '')

  const user = identity?.user

  return (
    <div className="app">
      <header className="header">
        <div className="logo">
          <span className="mark">⚡</span> Serverless Agent Portal
        </div>
        <div className="spacer" />
        <label className="sub-picker" title="Azure subscription">
          <span className="sub-picker-label">Subscription</span>
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            disabled={loading || !!error || subscriptions.length === 0}
          >
            {loading && <option value="">Loading…</option>}
            {error && <option value="">Unavailable</option>}
            {!loading &&
              !error &&
              subscriptions.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
          </select>
        </label>
        <div className="user" title={user ? `${user.name} · ${user.username}` : 'Not signed in'}>
          {user ? initials(user.name || user.username) : '…'}
        </div>
        <button className="btn ghost sm" onClick={() => void signOut()} title="Sign out">
          Sign out
        </button>
      </header>

      <nav className="nav">
        <div className="group-label">Build</div>
        <NavLink className={itemClass} to="/" end>
          <span className="ico">🤖</span> Agents
        </NavLink>
      </nav>

      <main className="main">{children}</main>
    </div>
  )
}
