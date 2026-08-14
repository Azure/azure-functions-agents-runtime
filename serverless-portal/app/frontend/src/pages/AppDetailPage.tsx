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

type Sel =
  | { kind: 'overview' }
  | { kind: 'agent'; name: string }
  | { kind: 'mcp'; name: string }
  | { kind: 'file'; path: string; label: string }

// AI App detail — a single Azure Function App identified as an AI App by the
// AZURE_FUNCTIONS_AGENTS_PROVIDER app setting. Shows its composition, endpoints,
// and source code, and lets the user deploy portal edits.
export default function AppDetailPage() {
  const { subscriptionId, app: appName } = useParams<{ subscriptionId: string; app: string }>()
  const navigate = useNavigate()
  const { selected, setSelected } = useIdentity()
  const deployJob = useDeployJob()
  const [sel, setSel] = useState<Sel>({ kind: 'overview' })

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
  const builtinCount = app?.agents.filter((a) => a.builtinEndpoints).length ?? 0
  const supportingCount = app?.supportingFunctions?.length ?? 0

  // mcp.json is only fetched when the MCP tab is opened.
  const { data: mcpSource } = useQuery({
    queryKey: ['source', subForQuery, appName ?? '', 'mcp.json'],
    queryFn: () =>
      api.getSource({ subscription: subForQuery, app: appName!, resourceGroup: app!.resourceGroup, path: 'mcp.json' }),
    enabled: !!app,
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

          <div className="components">
            <aside className="explorer">
              <button
                className={'node' + (sel.kind === 'overview' ? ' active' : '')}
                onClick={() => setSel({ kind: 'overview' })}
              >
                📊 Overview
              </button>

              <div className="group-label">Agents</div>
              {app.agents.map((a) => (
                <button
                  key={a.name}
                  className={'node' + (sel.kind === 'agent' && sel.name === a.name ? ' active' : '')}
                  onClick={() => setSel({ kind: 'agent', name: a.name })}
                  title={`${a.name}.agent.md`}
                >
                  📄 <span className="mono">{a.name}.agent.md</span>
                </button>
              ))}

              {mcpServers.length > 0 && (
                <>
                  <div className="group-label">MCP servers</div>
                  {mcpServers.map((s) => (
                    <button
                      key={'mcp:' + s.name}
                      className={'node' + (sel.kind === 'mcp' && sel.name === s.name ? ' active' : '')}
                      onClick={() => setSel({ kind: 'mcp', name: s.name })}
                      title={s.url || s.name}
                    >
                      🧰 <span className="mono">{s.name}</span>
                      {s.type && (
                        <span className="badge gray" style={{ marginLeft: 'auto' }}>
                          {s.type}
                        </span>
                      )}
                    </button>
                  ))}
                </>
              )}

              {app.supportingFunctions && app.supportingFunctions.length > 0 && (
                <>
                  <div className="group-label">Tools / triggers</div>
                  {app.supportingFunctions.map((fn) => (
                    <button
                      key={'fn:' + fn.name}
                      className={
                        'node' +
                        (sel.kind === 'file' && sel.path === 'function_app.py' && sel.label === fn.name
                          ? ' active'
                          : '')
                      }
                      onClick={() => setSel({ kind: 'file', path: 'function_app.py', label: fn.name })}
                      title={`${fn.trigger} trigger · defined in function_app.py`}
                    >
                      🐍 <span className="mono">{fn.name}</span>
                      <span className="badge gray" style={{ marginLeft: 'auto' }}>
                        {fn.trigger}
                      </span>
                    </button>
                  ))}
                </>
              )}

              <div className="group-label">App files</div>
              {codeFiles.map((f) => (
                <button
                  key={f.path}
                  className={
                    'node' +
                    (sel.kind === 'file' && sel.path === f.path && sel.label === f.label ? ' active' : '')
                  }
                  onClick={() => setSel({ kind: 'file', path: f.path, label: f.label })}
                  title={f.path}
                >
                  {f.icon} <span className="mono">{f.label}</span>
                </button>
              ))}
            </aside>

            <section className="component-editor">
              {sel.kind === 'overview' && (
                <>
                  <div className="card-head">
                    <h3 style={{ margin: 0 }}>Overview</h3>
                    <Badge tone="purple" title="AZURE_FUNCTIONS_AGENTS_PROVIDER app setting">
                      🔖 agent-runtime
                    </Badge>
                  </div>
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
                  <div className="divider" />
                  <span className="group-sub">Composition</span>
                  <StatTiles
                    items={[
                      { n: app.agents.length, label: app.agents.length === 1 ? 'Agent' : 'Agents' },
                      { n: builtinCount, label: 'Built-in' },
                      { n: supportingCount, label: 'Tools/triggers' },
                    ]}
                  />
                  <p className="muted" style={{ fontSize: 12, marginTop: 12 }}>
                    Select an agent, MCP server, or file on the left to view and edit its source.
                  </p>
                </>
              )}

              {sel.kind === 'agent' && (
                <>
                  <div className="card-head">
                    <h3 className="mono" style={{ margin: 0 }}>
                      {sel.name}.agent.md
                    </h3>
                    <Link
                      className="btn sm"
                      to={`/agents/${enc(subForQuery)}/${enc(app.name)}/${enc(sel.name)}`}
                      title="Open the full agent page"
                    >
                      Open agent page →
                    </Link>
                  </div>
                  <DraftEditor
                    key={'agent:' + sel.name}
                    queryKey={['agentDefinition', subForQuery, app.name, sel.name]}
                    load={() =>
                      api.getAgentDefinition({
                        subscription: subForQuery,
                        app: app.name,
                        resourceGroup: app.resourceGroup,
                        name: sel.name,
                      })
                    }
                    save={(content) =>
                      api.saveAgentDefinition({
                        subscription: subForQuery,
                        app: app.name,
                        name: sel.name,
                        content,
                      })
                    }
                    fallback=""
                  />
                </>
              )}

              {sel.kind === 'mcp' && (
                <>
                  <div className="card-head">
                    <h3 className="mono" style={{ margin: 0 }}>
                      mcp.json
                    </h3>
                    <span className="muted" style={{ fontSize: 12 }}>
                      server <span className="mono">{sel.name}</span> is defined here
                    </span>
                  </div>
                  <DraftEditor
                    key="mcp:mcp.json"
                    queryKey={['source', subForQuery, app.name, 'mcp.json']}
                    load={() =>
                      api.getSource({
                        subscription: subForQuery,
                        app: app.name,
                        resourceGroup: app.resourceGroup,
                        path: 'mcp.json',
                      })
                    }
                    save={(content) =>
                      api.saveSource({ subscription: subForQuery, app: app.name, path: 'mcp.json', content })
                    }
                    fallback=""
                  />
                </>
              )}

              {sel.kind === 'file' && (
                <>
                  <div className="card-head">
                    <h3 className="mono" style={{ margin: 0 }}>
                      {sel.path}
                    </h3>
                    {sel.label !== sel.path ? (
                      <span className="muted" style={{ fontSize: 12 }}>
                        <span className="mono">{sel.label}</span> is defined here
                      </span>
                    ) : (
                      <span className="badge blue">source</span>
                    )}
                  </div>
                  <DraftEditor
                    key={'file:' + sel.path}
                    queryKey={['source', subForQuery, app.name, sel.path]}
                    load={() =>
                      api.getSource({
                        subscription: subForQuery,
                        app: app.name,
                        resourceGroup: app.resourceGroup,
                        path: sel.path,
                      })
                    }
                    save={(content) =>
                      api.saveSource({ subscription: subForQuery, app: app.name, path: sel.path, content })
                    }
                    fallback=""
                  />
                </>
              )}
            </section>
          </div>
        </>
      )}
    </>
  )
}
