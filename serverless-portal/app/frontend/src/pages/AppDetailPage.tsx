import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, type LiveAgentApp } from '../api'
import { useDeployJob, DeploymentStatus } from '../deploy'
import { CopyButton, DraftEditor } from '../components/SourceEditor'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot, writeAgentsSnapshot } from '../query'
import { Badge, EmptyState, StatTiles, StatusBadge } from '../components/ui'

const enc = encodeURIComponent

interface Endpoint {
  label: string
  url: string
  kind: string
}

// Aggregate the callable URLs across every agent in the app: built-in chat
// endpoints (registration/endpoints.py conventions) plus any custom HTTP routes.
// The shared MCP webhook is listed once.
function buildAppEndpoints(app: LiveAgentApp): Endpoint[] {
  const host = app.defaultHostName
  if (!host) return []
  const base = `https://${host}`
  const out: Endpoint[] = []
  let mcpAdded = false
  for (const a of app.agents) {
    if (a.builtinEndpoints) {
      const name = enc(a.name)
      out.push({ label: `${a.name} · chat UI`, url: `${base}/agents/${name}/`, kind: 'GET' })
      out.push({ label: `${a.name} · chat API`, url: `${base}/agents/${name}/chat`, kind: 'POST' })
      if (!mcpAdded) {
        out.push({ label: 'MCP', url: `${base}/runtime/webhooks/mcp`, kind: 'POST' })
        mcpAdded = true
      }
    }
    for (const route of a.routes ?? []) {
      out.push({ label: `${a.name} · HTTP`, url: `${base}/${String(route).replace(/^\//, '')}`, kind: 'POST' })
    }
  }
  return out
}

interface CodeFile {
  path: string
  label: string
  icon: string
}

// A curated set of the app's authoring files. Live discovery can't list the
// deployment package tree, but these are the standard runtime files; each is
// read on demand via the ranged getSource API (missing ones render an editable
// blank via the DraftEditor's "not readable" path).
function buildCodeFiles(app: LiveAgentApp): CodeFile[] {
  const files: CodeFile[] = [{ path: 'function_app.py', label: 'function_app.py', icon: '🐍' }]
  for (const a of app.agents) {
    files.push({ path: `${a.name}.agent.md`, label: `${a.name}.agent.md`, icon: '📄' })
  }
  files.push({ path: 'mcp.json', label: 'mcp.json', icon: '🔌' })
  files.push({ path: 'agents.config.yaml', label: 'agents.config.yaml', icon: '⚙️' })
  files.push({ path: 'host.json', label: 'host.json', icon: '⚙️' })
  files.push({ path: 'requirements.txt', label: 'requirements.txt', icon: '📦' })
  return files
}

interface McpServer {
  name: string
  type: string
  url: string
}

function parseMcpServers(content?: string | null): McpServer[] {
  if (!content) return []
  try {
    const doc = JSON.parse(content) as { servers?: Record<string, { type?: string; url?: string }> }
    const servers = doc.servers ?? {}
    return Object.entries(servers).map(([name, v]) => ({
      name,
      type: String(v?.type ?? ''),
      url: String(v?.url ?? ''),
    }))
  } catch {
    return []
  }
}

type Tab = 'overview' | 'agents' | 'triggers' | 'mcp' | 'code'

// AI App detail — a single Azure Function App identified as an AI App by the
// AZURE_FUNCTIONS_AGENTS_PROVIDER app setting. Shows its composition, endpoints,
// and source code, and lets the user deploy portal edits.
export default function AppDetailPage() {
  const { subscriptionId, app: appName } = useParams<{ subscriptionId: string; app: string }>()
  const navigate = useNavigate()
  const { selected, setSelected } = useIdentity()
  const deployJob = useDeployJob()
  const [tab, setTab] = useState<Tab>('overview')
  const [codePath, setCodePath] = useState('function_app.py')

  useEffect(() => {
    if (subscriptionId && subscriptionId !== selected) {
      setSelected(subscriptionId)
    }
  }, [subscriptionId, selected, setSelected])

  const subForQuery = subscriptionId || selected
  const snapshot = useMemo(() => readAgentsSnapshot(subForQuery), [subForQuery])
  const { data, error: queryError, isFetching, dataUpdatedAt } = useQuery({
    queryKey: queryKeys.liveAgents(subForQuery),
    queryFn: () => api.liveAgents(subForQuery),
    enabled: !!subForQuery,
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnReconnect: false,
    initialData: snapshot?.data,
    initialDataUpdatedAt: snapshot?.updatedAt,
  })

  useEffect(() => {
    if (subForQuery && data) {
      writeAgentsSnapshot(subForQuery, data, dataUpdatedAt)
    }
  }, [subForQuery, data, dataUpdatedAt])

  const app: LiveAgentApp | undefined = useMemo(
    () => data?.apps.find((a) => a.name === appName),
    [data, appName],
  )

  const error = queryError ? (queryError as Error).message : null
  const scanning = !!subForQuery && !data && !error
  const backTo = `/agents/${subscriptionId ?? selected}`

  const endpoints = app ? buildAppEndpoints(app) : []
  const endpointsText = endpoints.map((e) => `${e.kind.padEnd(5)} ${e.url}`).join('\n')
  const codeFiles = app ? buildCodeFiles(app) : []
  const connectorAgents = app?.agents.filter((a) => a.trigger === 'connector') ?? []
  const builtinCount = app?.agents.filter((a) => a.builtinEndpoints).length ?? 0
  const supportingCount = app?.supportingFunctions?.length ?? 0

  // mcp.json is only fetched when the MCP tab is opened.
  const { data: mcpSource } = useQuery({
    queryKey: ['source', subForQuery, appName ?? '', 'mcp.json'],
    queryFn: () =>
      api.getSource({ subscription: subForQuery, app: appName!, resourceGroup: app!.resourceGroup, path: 'mcp.json' }),
    enabled: !!app && tab === 'mcp',
    staleTime: Infinity,
    refetchOnMount: false,
  })
  const mcpServers = parseMcpServers(mcpSource?.content)

  return (
    <>
      <div className="breadcrumb">
        Home / <Link to={backTo}>AI Apps</Link> / {appName}
      </div>
      <div className="page-title">
        <button className="btn ghost sm" onClick={() => navigate(backTo)} title="Back to AI Apps">
          ← Back
        </button>
        <h1 className="mono">{appName}</h1>
        {app && <StatusBadge status="running" />}
        {app?.provider && (
          <Badge tone="blue" title="AZURE_FUNCTIONS_AGENTS_PROVIDER">
            {app.provider}
          </Badge>
        )}
      </div>

      {scanning && <p className="page-sub">Scanning subscription…</p>}
      {error && <p className="page-sub">Failed to load app: {error}</p>}
      {data && !app && !scanning && (
        <EmptyState>
          AI App <strong>{appName}</strong> was not found in this subscription.{' '}
          <Link to={backTo}>Return to the dashboard</Link>.
        </EmptyState>
      )}

      {app && (
        <>
          <p className="page-sub">
            Azure Function App running the agent runtime
            {app.defaultHostName && (
              <>
                {' — '}
                <a href={`https://${app.defaultHostName}/`} target="_blank" rel="noreferrer">
                  Open host →
                </a>
              </>
            )}
          </p>

          <div className="toolbar" style={{ marginBottom: 12 }}>
            <button
              className="btn primary"
              disabled={deployJob.phase === 'running'}
              onClick={() =>
                deployJob.redeploy({
                  subscription: subForQuery,
                  resourceGroup: app.resourceGroup,
                  app: app.name,
                })
              }
              title="Redeploy this app from its current source with your saved drafts applied"
            >
              {deployJob.phase === 'running' ? 'Deploying…' : '🚀 Deploy edits'}
            </button>
            <Link className="btn" to="/create-agent">
              ＋ Add agent
            </Link>
            {isFetching && (
              <span className="cache-stamp">⟳ Refreshing…</span>
            )}
          </div>
          <DeploymentStatus
            phase={deployJob.phase}
            result={deployJob.result}
            portalUrl={deployJob.portalUrl}
            message={deployJob.message}
          />

          {endpoints.length > 0 && (
            <div className="card" style={{ marginBottom: 18 }}>
              <div className="card-head">
                <h3 style={{ margin: 0 }}>Endpoints</h3>
                <CopyButton text={endpointsText} title="Copy all URLs" />
              </div>
              <div className="endpoint-list">
                {endpoints.map((e) => (
                  <div className="endpoint-row" key={e.url}>
                    <span className={'badge ' + (e.kind === 'GET' ? 'gray' : 'purple')}>{e.kind}</span>
                    <span className="cell-title">{e.label}</span>
                    <code className="endpoint-url">{e.url}</code>
                    <CopyButton text={e.url} title={`Copy ${e.label} URL`} />
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="tabs" style={{ marginBottom: 16 }}>
            <button className={'tab' + (tab === 'overview' ? ' active' : '')} onClick={() => setTab('overview')}>
              Overview
            </button>
            <button className={'tab' + (tab === 'agents' ? ' active' : '')} onClick={() => setTab('agents')}>
              Agents ({app.agents.length})
            </button>
            <button className={'tab' + (tab === 'triggers' ? ' active' : '')} onClick={() => setTab('triggers')}>
              Connector triggers ({connectorAgents.length})
            </button>
            <button className={'tab' + (tab === 'mcp' ? ' active' : '')} onClick={() => setTab('mcp')}>
              MCP
            </button>
            <button className={'tab' + (tab === 'code' ? ' active' : '')} onClick={() => setTab('code')}>
              Code
            </button>
          </div>

          {tab === 'overview' && (
            <div className="grid cols-2">
              <div className="card">
                <h3>Details</h3>
                <dl className="meta-grid">
                  <dt>Function App</dt>
                  <dd className="mono">{app.name}</dd>
                  <dt>Resource group</dt>
                  <dd>{app.resourceGroup || '—'}</dd>
                  <dt>Region</dt>
                  <dd>{app.location || '—'}</dd>
                  <dt>Provider</dt>
                  <dd>
                    <span className="badge gray">{app.provider || '—'}</span>
                  </dd>
                  <dt>Host name</dt>
                  <dd className="mono">{app.defaultHostName || '—'}</dd>
                </dl>
              </div>
              <div className="card">
                <h3>Composition</h3>
                <StatTiles
                  items={[
                    { n: app.agents.length, label: app.agents.length === 1 ? 'Agent' : 'Agents' },
                    { n: connectorAgents.length, label: 'Triggers' },
                    { n: builtinCount, label: 'Built-in' },
                    { n: supportingCount, label: 'Supporting' },
                  ]}
                />
                <div className="divider" />
                <span className="group-sub">Identified by</span>
                <div className="chips">
                  <Badge tone="purple" title="AZURE_FUNCTIONS_AGENTS_PROVIDER app setting">
                    🔖 agent-runtime
                  </Badge>
                </div>
              </div>
            </div>
          )}

          {tab === 'agents' && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Agent</th>
                    <th>Trigger</th>
                    <th>Built-in endpoints</th>
                  </tr>
                </thead>
                <tbody>
                  {app.agents.map((a) => (
                    <tr key={a.name}>
                      <td>
                        <div className="cell-title">
                          <Link
                            to={`/agents/${enc(subForQuery)}/${enc(app.name)}/${enc(a.name)}`}
                          >
                            {a.name}
                          </Link>
                        </div>
                      </td>
                      <td>
                        {a.trigger === 'none' ? (
                          <span className="badge gray">no trigger</span>
                        ) : (
                          <span className="badge blue">{a.trigger || 'http'}</span>
                        )}
                      </td>
                      <td>
                        {a.builtinEndpoints ? (
                          <span className="badge green">
                            <span className="dot" /> enabled
                          </span>
                        ) : (
                          <span className="muted">disabled</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {tab === 'triggers' && (
            <div className="table-wrap">
              {connectorAgents.length > 0 ? (
                <table>
                  <thead>
                    <tr>
                      <th>Agent</th>
                      <th>Trigger</th>
                    </tr>
                  </thead>
                  <tbody>
                    {connectorAgents.map((a) => (
                      <tr key={a.name}>
                        <td className="cell-title mono">{a.name}</td>
                        <td>
                          <span className="badge blue">connector</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <EmptyState>
                  No connector triggers. Add one to an agent from its detail page, or in the Code tab
                  (a <span className="mono">generic_trigger · connectorTrigger</span> in the agent’s
                  <span className="mono"> .agent.md</span>).
                </EmptyState>
              )}
            </div>
          )}

          {tab === 'mcp' && (
            <div className="table-wrap">
              {mcpServers.length > 0 ? (
                <table>
                  <thead>
                    <tr>
                      <th>Server</th>
                      <th>Type</th>
                      <th>URL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mcpServers.map((s) => (
                      <tr key={s.name}>
                        <td className="cell-title mono">{s.name}</td>
                        <td>
                          <span className="badge gray">{s.type || '—'}</span>
                        </td>
                        <td className="cell-sub mono">{s.url || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <EmptyState>
                  No MCP servers found in <span className="mono">mcp.json</span>. Open the Code tab to add
                  one.
                </EmptyState>
              )}
            </div>
          )}

          {tab === 'code' && (
            <div className="components">
              <aside className="explorer">
                <div className="group-label">App files</div>
                {codeFiles.map((f) => (
                  <button
                    key={f.path}
                    className={'node' + (codePath === f.path ? ' active' : '')}
                    onClick={() => setCodePath(f.path)}
                    title={f.path}
                  >
                    {f.icon} <span className="mono">{f.label}</span>
                  </button>
                ))}
              </aside>
              <section className="component-editor">
                <div className="card-head">
                  <h3 className="mono" style={{ margin: 0 }}>
                    {codePath}
                  </h3>
                  <span className="badge blue">source</span>
                </div>
                <DraftEditor
                  key={'code:' + codePath}
                  queryKey={['source', subForQuery, app.name, codePath]}
                  load={() =>
                    api.getSource({
                      subscription: subForQuery,
                      app: app.name,
                      resourceGroup: app.resourceGroup,
                      path: codePath,
                    })
                  }
                  save={(content) =>
                    api.saveSource({ subscription: subForQuery, app: app.name, path: codePath, content })
                  }
                  fallback=""
                />
              </section>
            </div>
          )}
        </>
      )}
    </>
  )
}
