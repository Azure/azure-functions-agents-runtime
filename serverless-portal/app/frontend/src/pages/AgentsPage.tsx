import { useEffect, useMemo } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQueries, useQuery } from '@tanstack/react-query'
import { api, type LiveAgentApp } from '../api'
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
// Hosted Skills app?" signal), scoped to the selected subscription. Each app is
// a flat row linking through to the app detail page.
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
          loading={identityLoading}
          error={!!identityError}
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
          ＋ New App
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
          {apps.map((app) => (
            <HostedSkillRow
              key={app.name}
              app={app}
              renderAppLink={(children) => (
                <Link to={`/apps/${encodeURIComponent(selected)}/${encodeURIComponent(app.name)}`}>{children}</Link>
              )}
            />
          ))}
        </div>
      )}

      {apps.length > 0 && <DashboardFailuresPanel subscription={selected} apps={apps} />}
    </>
  )
}

// Empty-state on the dashboard — three top-level "get started" tiles above a
// gallery of runtime samples. Explicit chooser is friendlier for first-time
// visitors than a bare "no apps" message, and it turns the samples/ folder
// into a real onboarding surface instead of docs the user has to hunt down.
function FirstRunEmptyState({ subName }: { subName: string }) {
  return (
    <div style={{ marginTop: 16 }}>
      <div className="first-run-hero">
        <h2 style={{ margin: '0 0 6px' }}>Nothing running here yet.</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          No Hosted Skills in {subName || 'this subscription'}. Pick a starting point:
        </p>
        <div className="first-run-tiles">
          <Link to="/create-agent?tab=sample" className="first-run-tile">
            <span className="first-run-icon">🎁</span>
            <span className="first-run-title">Deploy a sample</span>
            <span className="first-run-blurb">One-click starter apps for chat, timer, connector, and workflow agents.</span>
          </Link>
          <Link to="/create-agent" className="first-run-tile">
            <span className="first-run-icon">✨</span>
            <span className="first-run-title">Create from scratch</span>
            <span className="first-run-blurb">Describe a Hosted Skill and Foundry writes its <span className="mono">.agent.md</span>.</span>
          </Link>
          <Link to="/create-agent?tab=github" className="first-run-tile">
            <span className="first-run-icon">🐙</span>
            <span className="first-run-title">Import from GitHub</span>
            <span className="first-run-blurb">Point at a repo with a runtime app and deploy it in place.</span>
          </Link>
        </div>
      </div>
      <SampleGallery />
    </div>
  )
}

// Sample gallery — a horizontal card row that lists the runtime's bundled
// samples. Clicking a card deep-links into the Create wizard with the sample
// pre-selected so the user lands on a ready-to-deploy draft.
function SampleGallery() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['samples'],
    queryFn: () => api.listSamples(),
    staleTime: 60 * 60 * 1000,
  })
  if (isLoading) return null
  if (error) return null
  const samples = data?.samples ?? []
  if (samples.length === 0) return null
  return (
    <div style={{ marginTop: 26 }}>
      <div className="card-head">
        <h3 style={{ margin: 0 }}>Start from a sample</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          Each sample is ready to run — deploy one, then edit in the portal.
        </span>
      </div>
      <div className="sample-grid">
        {samples.map((s) => (
          <Link
            key={s.slug}
            className="sample-card"
            to={`/create-agent?sample=${encodeURIComponent(s.slug)}`}
            title={s.blurb || s.title}
          >
            <div className="sample-title">
              <span className="mono">{s.slug}</span>
            </div>
            <div className="sample-blurb">{s.blurb || s.title}</div>
            <div className="sample-chips">
              {s.triggerTypes.map((t) => (
                <span className="cap-chip cap-chip-ok" key={t}>
                  {t}
                </span>
              ))}
              {s.hasMcp && <span className="cap-chip cap-chip-ok">mcp</span>}
              {s.hasSkills && <span className="cap-chip cap-chip-ok">skills</span>}
              {s.hasWorkflow && <span className="cap-chip cap-chip-ok">workflow</span>}
              {s.agents.length > 1 && (
                <span className="cap-chip cap-chip-ok">multi-agent · {s.agents.length}</span>
              )}
            </div>
          </Link>
        ))}
      </div>
    </div>
  )
}

// Recent-failures panel on the dashboard — aggregates the last 24h of failed
// invocations across every Hosted Skills app in the selected subscription. Fires one
// App Insights query per app in parallel; skipping any app whose caller can't
// reach its component (403 / no linked AI). Each failure links out to the
// Transaction Search blade for that specific operation.
function DashboardFailuresPanel({ subscription, apps }: { subscription: string; apps: LiveAgentApp[] }) {
  const queries = useQueries({
    queries: apps.map((app) => ({
      queryKey: ['dashFailures', subscription, app.resourceGroup, app.name],
      queryFn: () =>
        api.appInsightsQuery({
          subscription,
          resourceGroup: app.resourceGroup,
          app: app.name,
          preset: 'recentFailures' as const,
          timeRange: '24h',
        }),
      staleTime: 5 * 60 * 1000,
      retry: false,
    })),
  })
  const anyLoading = queries.some((q) => q.isLoading)
  const rows = apps.flatMap((app, i) => {
    const q = queries[i]
    if (!q.data?.tables?.[0]) return []
    const table = q.data.tables[0]
    return table.rows.map((row) => ({
      app: app.name,
      componentId: q.data?.componentId ?? '',
      row,
      columns: table.columns,
    }))
  })
  rows.sort((a, b) => {
    const at = String(pickCol(a.columns, a.row, 'timestamp') ?? '')
    const bt = String(pickCol(b.columns, b.row, 'timestamp') ?? '')
    return bt.localeCompare(at)
  })

  const top = rows.slice(0, 10)

  return (
    <div className="card" style={{ marginTop: 24 }}>
      <div className="card-head">
        <h3 style={{ margin: 0 }}>Recent failures (24h)</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          {anyLoading ? 'Loading…' : `${rows.length} total across ${apps.length} apps`}
        </span>
      </div>
      {top.length === 0 ? (
        <p className="muted" style={{ fontSize: 13 }}>
          No failures reported by App Insights in the last 24 hours across these apps.
        </p>
      ) : (
        <div className="ai-table-wrap">
          <table className="ai-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>App</th>
                <th>Operation</th>
                <th>Result</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {top.map((f, i) => {
                const operationId = String(pickCol(f.columns, f.row, 'operation_Id') ?? '')
                const link = operationId && f.componentId
                  ? `https://portal.azure.com/#resource${f.componentId}/searchV1/searchTerm/${encodeURIComponent(operationId)}`
                  : ''
                return (
                  <tr key={`${f.app}:${i}`}>
                    <td>{formatTimestamp(pickCol(f.columns, f.row, 'timestamp'))}</td>
                    <td className="mono">{f.app}</td>
                    <td className="mono">{String(pickCol(f.columns, f.row, 'name') ?? '')}</td>
                    <td>{String(pickCol(f.columns, f.row, 'resultCode') ?? '')}</td>
                    <td>
                      {link && (
                        <a href={link} target="_blank" rel="noreferrer" className="btn sm">
                          Open trace →
                        </a>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function pickCol(cols: { name: string }[], row: unknown[], name: string): unknown {
  const i = cols.findIndex((c) => c.name === name)
  return i >= 0 ? row[i] : undefined
}
function formatTimestamp(v: unknown): string {
  if (typeof v !== 'string') return '—'
  try {
    return new Date(v).toLocaleString()
  } catch {
    return v
  }
}
