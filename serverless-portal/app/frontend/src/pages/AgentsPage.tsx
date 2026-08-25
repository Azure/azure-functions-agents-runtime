import { useEffect, useMemo } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot, writeAgentsSnapshot } from '../query'
import { EmptyState, HostedSkillRow, StatTiles, SubscriptionPicker } from '../components/ui'
import { Button } from '@coreai/fluentui-react'

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
// The dashboard: Azure Function Apps identified as Hosted Skills apps by the
// `AZURE_FUNCTIONS_AGENTS_PROVIDER` app setting (the backend's sole "is this an
// Hosted Skills app?" signal), scoped to the selected subscription. Each skill
// is a flat row linking through to its detail page.
export default function AgentsPage() {
  const {
    subscriptions,
    selected,
    setSelected,
    subscriptionsLoading,
    subscriptionsRefreshing,
    subscriptionError,
    refreshSubscriptions,
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
      <div className="breadcrumb">Home / Hosted Skills</div>
      <div className="page-title">
        <h1>Hosted Skills</h1>
      </div>
      <p className="page-sub">
        Create and manage apps that host AI skills on Azure Functions in <strong>{subName}</strong>
        {data
          ? ` — ${apps.length} app${apps.length === 1 ? '' : 's'}, ${agents.length} Hosted Skill${agents.length === 1 ? '' : 's'}`
          : ''}
        .
      </p>

      <div className="toolbar">
        <SubscriptionPicker
          subscriptions={subscriptions}
          value={selected}
          onChange={onPickSubscription}
          loading={subscriptionsLoading}
          refreshing={subscriptionsRefreshing}
          error={!!subscriptionError}
          onRetry={() => void refreshSubscriptions()}
        />
        {data && (
          <span className="cache-stamp" title="When this subscription's Hosted Skills were last fetched">
            Cached {formatCachedAt(dataUpdatedAt)}
          </span>
        )}
        <Button
          onClick={() => refetch()}
          disabled={!selected || isFetching}
          title="Force a fresh scan of the selected subscription"
        >
          {isFetching ? '⟳ Refreshing…' : '⟳ Hard refresh'}
        </Button>
        <Link className="btn primary" to="/create-agent">
          ＋ New Skill
        </Link>
      </div>

      {data && apps.length > 0 && (
        <StatTiles
          items={[
            { n: apps.length, label: apps.length === 1 ? 'App' : 'Apps' },
            { n: agents.length, label: agents.length === 1 ? 'Hosted Skill' : 'Hosted Skills' },
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
      {data && apps.length === 0 && <FirstRunEmptyState subName={subName} />}

      {apps.length > 0 && (
        <div className="hosted-skill-table" style={{ marginTop: 16 }}>
          <div className="hosted-skill-table-head" aria-hidden="true">
            <span>App</span><span>Hosted Skills</span><span>Model</span><span>Region</span><span>Health</span><span />
          </div>
          {apps.flatMap((app) => {
            if (app.agents.length === 0) {
              return (
                <HostedSkillRow
                  key={app.name}
                  app={app}
                  renderAppLink={(children) => (
                    <Link to={`/apps/${encodeURIComponent(selected)}/${encodeURIComponent(app.name)}`}>{children}</Link>
                  )}
                />
              )
            }

            return app.agents.map((agent) => (
              <HostedSkillRow
                key={`${app.name}/${agent.name}`}
                app={{ ...app, agents: [agent] }}
                renderAppLink={(children) => (
                  <Link to={`/agents/${encodeURIComponent(selected)}/${encodeURIComponent(app.name)}/${encodeURIComponent(agent.name)}`}>
                    {children}
                  </Link>
                )}
              />
            ))
          })}
        </div>
      )}

    </>
  )
}
function FirstRunEmptyState({ subName }: { subName: string }) {
  return (
    <div className="empty" style={{ marginTop: 16 }}>
      <h2 style={{ margin: '0 0 6px' }}>No Hosted Skills yet</h2>
      <p className="muted">Create the first skill in {subName || 'this subscription'}.</p>
      <Link to="/create-agent" className="btn primary">＋ New Skill</Link>
    </div>
  )
}

