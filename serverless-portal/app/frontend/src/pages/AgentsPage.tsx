import { useEffect, useMemo } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot, writeAgentsSnapshot } from '../query'
import { AiAppCard, EmptyState, StatTiles, SubscriptionPicker } from '../components/ui'

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

// The dashboard: Azure Function Apps identified as AI Apps by the
// `AZURE_FUNCTIONS_AGENTS_PROVIDER` app setting (the backend's sole "is this an
// agent app?" signal), scoped to the selected subscription. Each app is a card
// listing its agents, which link through to the agent detail page.
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
  // `/agents/:subscriptionId` restores the exact view.
  useEffect(() => {
    if (subscriptionId && subscriptionId !== selected) {
      setSelected(subscriptionId)
    }
  }, [subscriptionId, selected, setSelected])

  // State → deeplink: keep the URL in sync with the selected subscription.
  useEffect(() => {
    if (selected && selected !== subscriptionId) {
      navigate(`/agents/${selected}`, { replace: true })
    }
  }, [selected, subscriptionId, navigate])

  // Live discovery is cached per subscription and persisted to localStorage, so
  // a shared/reloaded deeplink hydrates instantly. It never auto-refetches — a
  // scan only runs on the first load of an uncached subscription or a Hard refresh.
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

  useEffect(() => {
    if (selected && data) {
      writeAgentsSnapshot(selected, data, dataUpdatedAt)
    }
  }, [selected, data, dataUpdatedAt])

  const error = queryError ? (queryError as Error).message : null
  const apps = data?.apps ?? []
  const agents = data?.agents ?? []
  const builtinCount = agents.filter((a) => a.builtinEndpoints).length
  const scanning = !!selected && !data && !error
  const subName = subscriptions.find((s) => s.id === selected)?.name ?? 'the subscription'

  const onPickSubscription = (id: string) => {
    setSelected(id)
    navigate(`/agents/${id}`)
  }

  return (
    <>
      <div className="breadcrumb">Home / AI Apps</div>
      <div className="page-title">
        <h1>AI Apps</h1>
      </div>
      <p className="page-sub">
        AI Apps are Azure Function Apps that run the agent runtime, discovered in <strong>{subName}</strong>
        {data
          ? ` — ${apps.length} app${apps.length === 1 ? '' : 's'}, ${agents.length} agent${agents.length === 1 ? '' : 's'}`
          : ''}
        .
      </p>

      <div className="toolbar">
        <SubscriptionPicker
          subscriptions={subscriptions}
          value={selected}
          onChange={onPickSubscription}
          loading={identityLoading}
          error={!!identityError}
        />
        {data && (
          <span className="cache-stamp" title="When this subscription's AI Apps were last fetched">
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
        <Link className="btn primary" to="/create-agent">
          ＋ New AI App
        </Link>
      </div>

      {data && apps.length > 0 && (
        <StatTiles
          items={[
            { n: apps.length, label: apps.length === 1 ? 'AI App' : 'AI Apps' },
            { n: agents.length, label: agents.length === 1 ? 'Agent' : 'Agents' },
            { n: builtinCount, label: 'Built-in endpoints' },
          ]}
        />
      )}

      {isFetching && <div className="skeleton shimmer-bar" style={{ margin: '16px 0 12px' }} />}

      {scanning && (
        <div className="card-grid" style={{ marginTop: 16 }}>
          {Array.from({ length: 4 }).map((_, i) => (
            <div className="card" key={`sk-${i}`} aria-busy="true">
              <div className="skeleton skeleton-line lg" style={{ width: '62%' }} />
              <div className="skeleton skeleton-line sm" style={{ width: '42%' }} />
              <div className="skeleton skeleton-line" style={{ width: '84%', marginTop: 14 }} />
              <div className="skeleton skeleton-line" style={{ width: '70%' }} />
            </div>
          ))}
        </div>
      )}

      {error && <EmptyState>Failed to scan: {error}</EmptyState>}
      {data && apps.length === 0 && (
        <EmptyState>
          No AI Apps found in {subName}. Deploy one with <Link to="/create-agent">＋ New AI App</Link>, or pick
          another subscription.
        </EmptyState>
      )}

      {apps.length > 0 && (
        <div className="card-grid" style={{ marginTop: 16 }}>
          {apps.map((app) => (
            <AiAppCard
              key={app.name}
              app={app}
              renderAppLink={(children) => (
                <Link to={`/apps/${encodeURIComponent(selected)}/${encodeURIComponent(app.name)}`}>{children}</Link>
              )}
              renderAgent={(a) => (
                <Link
                  to={`/agents/${encodeURIComponent(selected)}/${encodeURIComponent(app.name)}/${encodeURIComponent(a.name)}`}
                >
                  {a.name}.agent.md
                </Link>
              )}
            />
          ))}
        </div>
      )}
    </>
  )
}
