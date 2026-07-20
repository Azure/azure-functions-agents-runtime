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
  const { identity } = useIdentity()

  const itemClass = ({ isActive }: { isActive: boolean }) => 'item' + (isActive ? ' active' : '')

  const user = identity?.user

  return (
    <div className="app">
      <header className="header">
        <div className="logo">
          <span className="mark">⚡</span> Serverless Agent Portal
        </div>
        <div className="spacer" />
        <div className="user" title={user ? `${user.name} · ${user.username}` : 'Not signed in'}>
          {user ? initials(user.name || user.username) : '…'}
        </div>
        <button className="btn ghost sm" onClick={() => void signOut()} title="Sign out">
          Sign out
        </button>
      </header>

      <nav className="nav">
        <div className="group-label">Build</div>
        <NavLink className={itemClass} to="/agents">
          <span className="ico">🤖</span> Agents
        </NavLink>
      </nav>

      <main className="main">{children}</main>
    </div>
  )
}
