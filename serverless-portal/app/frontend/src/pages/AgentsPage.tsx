import { useEffect, useMemo } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot, writeAgentsSnapshot } from '../query'

function formatCachedAt(ms: number): string {
  if (!ms) return ''
  const d = new Date(ms)
  const now = Date.now()
  const secs = Math.round((now - ms) / 1000)
  let rel: string
  if (secs < 5) rel = 'just now'
  else if (secs < 60) rel = `${secs}s ago`
  else if (secs < 3600) rel = `${Math.floor(secs / 60)}m ago`
  else rel = `${Math.floor(secs / 3600)}h ago`
  return `${d.toLocaleString()} (${rel})`
}

export default function AgentsPage() {
  const {
    subscriptions,
    selected,
    setSelected,
    loading: identityLoading,
    error: identityError,
  } = useIdentity()

  const { subscriptionId } = useParams<{ subscriptionId: string }>()
  const navigate = useNavigate()

  // Deeplink → state: adopt the subscription from the URL so a shared/reloaded
  // `/agents/:subscriptionId` restores the exact view (even before the
  // subscription list has loaded).
  useEffect(() => {
    if (subscriptionId && subscriptionId !== selected) {
      setSelected(subscriptionId)
    }
  }, [subscriptionId, selected, setSelected])

  // State → deeplink: keep the URL in sync with the selected subscription so it
  // is always shareable (replace so it doesn't spam browser history).
  useEffect(() => {
    if (selected && selected !== subscriptionId) {
      navigate(`/agents/${selected}`, { replace: true })
    }
  }, [selected, subscriptionId, navigate])

  // Live agent discovery is cached per subscription and persisted to
  // localStorage, so a shared/reloaded deeplink hydrates instantly from cache.
  // The data never auto-refetches (staleTime Infinity + refetchOnMount off) — a
  // network scan only happens on the first load of a subscription with no
  // cached snapshot, or when the user presses Hard refresh.
  const snapshot = useMemo(() => readAgentsSnapshot(selected), [selected])
  const {
    data,
    error: queryError,
    isFetching,
    refetch,
    dataUpdatedAt,
  } = useQuery({
    queryKey: queryKeys.liveAgents(selected),
    queryFn: () => api.liveAgents(selected),
    enabled: !!selected,
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnReconnect: false,
    initialData: snapshot?.data,
    initialDataUpdatedAt: snapshot?.updatedAt,
  })

  // Persist each successful scan (and its timestamp) so the deeplink survives
  // full page reloads.
  useEffect(() => {
    if (selected && data) {
      writeAgentsSnapshot(selected, data, dataUpdatedAt)
    }
  }, [selected, data, dataUpdatedAt])

  const error = queryError ? (queryError as Error).message : null
  const agents = data?.agents ?? []
  const scanning = !!selected && !data && !error
  const subName = subscriptions.find((s) => s.id === selected)?.name ?? 'the subscription'

  const onPickSubscription = (id: string) => {
    setSelected(id)
    navigate(`/agents/${id}`)
  }

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
            onChange={(e) => onPickSubscription(e.target.value)}
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
        {data && (
          <span className="cache-stamp" title="When this subscription's agents were last fetched">
            Cached {formatCachedAt(dataUpdatedAt)}
          </span>
        )}
        <button
          className="btn"
          onClick={() => refetch()}
          disabled={!selected || isFetching}
          title="Force a fresh scan of the selected subscription"
        >
          {isFetching ? '⟳ Refreshing…' : '⟳ Hard refresh'}
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
            {scanning && (
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
                  <div className="cell-title">
                    <Link
                      to={`/agents/${encodeURIComponent(selected)}/${encodeURIComponent(a.app)}/${encodeURIComponent(a.name)}`}
                    >
                      {a.name}
                    </Link>
                  </div>
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
                  ) : a.routes && a.routes.length > 0 ? (
                    <span className="badge gray">
                      {a.routes.length} route{a.routes.length === 1 ? '' : 's'}
                    </span>
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
