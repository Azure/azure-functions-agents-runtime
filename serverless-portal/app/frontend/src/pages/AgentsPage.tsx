import { useCallback, useEffect, useState } from 'react'
import { api, type LiveDiscovery } from '../api'
import { useIdentity } from '../identity'

export default function AgentsPage() {
  const {
    subscriptions,
    selected,
    setSelected,
    loading: identityLoading,
    error: identityError,
  } = useIdentity()
  const [data, setData] = useState<LiveDiscovery | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!selected) return
    setData(null)
    setError(null)
    try {
      setData(await api.liveAgents(selected))
    } catch (e) {
      setError((e as Error).message)
    }
  }, [selected])

  // Re-scan whenever the selected subscription changes.
  useEffect(() => {
    load()
  }, [load])

  const agents = data?.agents ?? []
  const subName = subscriptions.find((s) => s.id === selected)?.name ?? 'the subscription'

  return (
    <>
      <div className="breadcrumb">Home / Agents</div>
      <div className="page-title">
        <h1>Agents</h1>
      </div>
      <p className="page-sub">
        Serverless agents discovered in <strong>{subName}</strong>
        {data
          ? ` — ${agents.length} agent${agents.length === 1 ? '' : 's'} across ${data.apps.length} Function App${data.apps.length === 1 ? '' : 's'}`
          : ''}
        .
      </p>

      <div className="toolbar">
        <label className="sub-picker" title="Azure subscription">
          <span className="sub-picker-label">Subscription</span>
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            disabled={identityLoading || !!identityError || subscriptions.length === 0}
          >
            {identityLoading && <option value="">Loading…</option>}
            {identityError && <option value="">Unavailable</option>}
            {!identityLoading &&
              !identityError &&
              subscriptions.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
          </select>
        </label>
        <button className="btn" onClick={load} disabled={!selected}>
          ⟳ Refresh
        </button>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Agent</th>
              <th>Function App</th>
              <th>Resource group</th>
              <th>Provider</th>
              <th>Trigger</th>
              <th>Endpoints</th>
            </tr>
          </thead>
          <tbody>
            {data === null && !error && (
              <tr>
                <td colSpan={6} className="empty">
                  Scanning subscription…
                </td>
              </tr>
            )}
            {error && (
              <tr>
                <td colSpan={6} className="empty">
                  Failed to scan: {error}
                </td>
              </tr>
            )}
            {data && agents.length === 0 && (
              <tr>
                <td colSpan={6} className="empty">
                  No serverless agents found in this subscription.
                </td>
              </tr>
            )}
            {agents.map((a) => (
              <tr key={`${a.app}/${a.name}`}>
                <td>
                  <div className="cell-title">{a.name}</div>
                  {a.defaultHostName && (
                    <div className="cell-sub">
                      <a
                        href={`https://${a.defaultHostName}/agents/${encodeURIComponent(a.name)}/`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Open chat →
                      </a>
                    </div>
                  )}
                </td>
                <td className="mono">{a.app}</td>
                <td className="muted">{a.resourceGroup}</td>
                <td>
                  {a.provider ? (
                    <span className="badge gray">{a.provider}</span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>
                  <span className="badge blue">{a.trigger || 'http'}</span>
                </td>
                <td>
                  {a.builtinEndpoints ? (
                    <span className="badge gray">built-in</span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
