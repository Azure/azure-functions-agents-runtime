import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type AgentSummary } from '../api'

export default function AgentsPage() {
  const [agents, setAgents] = useState<AgentSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setAgents(null)
    setError(null)
    try {
      setAgents(await api.list())
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  return (
    <>
      <div className="breadcrumb">Home / Agents</div>
      <div className="page-title">
        <h1>Agents</h1>
      </div>
      <p className="page-sub">Agent files persisted in the Function App storage account (working copy).</p>

      <div className="toolbar">
        <Link className="btn primary" to="/create">
          ＋ Create agent
        </Link>
        <button className="btn" onClick={load}>
          ⟳ Refresh
        </button>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Trigger</th>
              <th>Endpoints</th>
              <th>Last modified</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {agents === null && !error && (
              <tr>
                <td colSpan={5} className="empty">
                  Loading…
                </td>
              </tr>
            )}
            {error && (
              <tr>
                <td colSpan={5} className="empty">
                  Failed to load: {error}
                </td>
              </tr>
            )}
            {agents && agents.length === 0 && (
              <tr>
                <td colSpan={5} className="empty">
                  No agents yet. <Link to="/create">Create your first agent →</Link>
                </td>
              </tr>
            )}
            {agents?.map((a) => (
              <tr key={a.name}>
                <td>
                  <div className="cell-title">{a.name}</div>
                  <div className="cell-sub">{a.description || a.displayName}</div>
                </td>
                <td>
                  <span className="badge blue">{a.trigger || 'http'}</span>
                </td>
                <td>
                  {a.builtinEndpoints ? (
                    <span className="badge gray">chat UI · API · MCP</span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td className="muted">
                  {a.lastModified ? new Date(a.lastModified).toLocaleString() : '—'}
                </td>
                <td>
                  <Link to={`/edit/${encodeURIComponent(a.name)}`}>Edit →</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
