import { useEffect, useState, type ReactNode } from 'react'
import { NavLink } from 'react-router-dom'
import { api } from '../api'

export default function Shell({ children }: { children: ReactNode }) {
  const [backend, setBackend] = useState('loading…')

  useEffect(() => {
    api
      .health()
      .then((h) => setBackend(`${h.project}/${h.environment} · ${h.storage}`))
      .catch(() => setBackend('storage: unreachable'))
  }, [])

  const itemClass = ({ isActive }: { isActive: boolean }) => 'item' + (isActive ? ' active' : '')

  return (
    <div className="app">
      <header className="header">
        <div className="logo">
          <span className="mark">⚡</span> Serverless Agent Portal
        </div>
        <div className="spacer" />
        <span className="env">{backend}</span>
        <div className="user">SN</div>
      </header>

      <nav className="nav">
        <div className="group-label">Build</div>
        <NavLink className={itemClass} to="/" end>
          <span className="ico">🤖</span> Agents
        </NavLink>
        <NavLink className={itemClass} to="/create">
          <span className="ico">＋</span> Create agent
        </NavLink>
      </nav>

      <main className="main">{children}</main>
    </div>
  )
}
