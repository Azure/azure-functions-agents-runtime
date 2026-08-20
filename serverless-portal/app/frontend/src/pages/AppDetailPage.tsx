import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type LiveAgentApp, type SourceListEntry } from '../api'
import { useDeployJob, DeploymentStatus, GitHubConnect } from '../deploy'
import { CopyButton, DraftEditor } from '../components/SourceEditor'
import { AddCapability } from '../components/AddCapability'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot, writeAgentsSnapshot } from '../query'
import { Badge, EmptyState, StatTiles, StatusBadge } from '../components/ui'
import { Button } from '@coreai/fluentui-react'

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

// Azure portal deep links to the Function App's monitoring surfaces. `Monitor`
// opens the Application Insights integration blade; `Failures` opens the App
// Insights Failures blade filtered to this app's role name. Both are scoped to
// the `gen_ai` + invocation signals the runtime already emits — the wider
// infra/health story lives in Azure Monitor.
interface AzureResourceCtx {
  tenantId?: string
  subscriptionId: string
  resourceGroup: string
  app: string
}
function azurePortalRoot(tenantId?: string): string {
  return `https://portal.azure.com/#${tenantId ? `@${tenantId}` : ''}`
}
function functionAppResourcePath(ctx: AzureResourceCtx): string {
  return `/resource/subscriptions/${ctx.subscriptionId}/resourceGroups/${ctx.resourceGroup}/providers/Microsoft.Web/sites/${ctx.app}`
}
function buildFunctionAppMonitorUrl(ctx: AzureResourceCtx): string {
  // AppInsights blade on the Function App — resolves the connected component
  // and lands on Live Metrics/Performance without a separate ARM lookup.
  return `${azurePortalRoot(ctx.tenantId)}${functionAppResourcePath(ctx)}/appServices`
}
function buildFunctionAppFailuresUrl(ctx: AzureResourceCtx): string {
  // App Insights "Failures" blade for this app's Function App. The blade
  // auto-filters to the linked component + this app's cloud role name.
  return `${azurePortalRoot(ctx.tenantId)}${functionAppResourcePath(ctx)}/failures`
}

// A folder in the sidebar's file tree. Files at the root live in `files`;
// nested directories live in `dirs`, keyed by folder name.
interface TreeFolder {
  files: TreeFile[]
  dirs: Map<string, TreeFolder>
}
interface TreeFile {
  path: string // wwwroot-relative
  name: string // basename
  source: 'draft' | 'deployed' | 'both' | 'stub'
}

function pickIcon(name: string): string {
  if (name.endsWith('.agent.md')) return '📄'
  if (name.endsWith('.py')) return '🐍'
  if (name === 'mcp.json') return '🔌'
  if (name === 'host.json' || name.endsWith('.yaml') || name.endsWith('.yml')) return '⚙️'
  if (name === 'requirements.txt' || name.startsWith('package')) return '📦'
  if (name.endsWith('.md')) return '💡'
  if (name.endsWith('.json')) return '🗂️'
  return '📄'
}

function newFolder(): TreeFolder {
  return { files: [], dirs: new Map() }
}

// Union of the discovered files and the curated fallback list into a
// nested-folder tree. Well-known files a fresh app is expected to have
// (function_app.py, host.json, …) are surfaced even when the deployed package
// can't be read, so the user can still start a draft.
function buildFileTree(
  listed: SourceListEntry[],
  fallback: { path: string; label: string; icon: string }[],
): TreeFolder {
  const root = newFolder()
  const byPath = new Map<string, TreeFile>()

  const insert = (file: TreeFile) => {
    byPath.set(file.path, file)
    const segments = file.path.split('/')
    let cursor = root
    for (let i = 0; i < segments.length - 1; i++) {
      const dir = segments[i]
      if (!cursor.dirs.has(dir)) cursor.dirs.set(dir, newFolder())
      cursor = cursor.dirs.get(dir)!
    }
    cursor.files.push(file)
  }

  for (const f of listed) {
    insert({
      path: f.path,
      name: f.path.split('/').pop() ?? f.path,
      source: f.source,
    })
  }
  for (const f of fallback) {
    if (byPath.has(f.path)) continue
    insert({
      path: f.path,
      name: f.path.split('/').pop() ?? f.path,
      source: 'stub',
    })
  }

  // Deterministic order: files A-Z, then subfolders A-Z.
  const sortFolder = (folder: TreeFolder) => {
    folder.files.sort((a, b) => a.name.localeCompare(b.name))
    folder.dirs = new Map([...folder.dirs.entries()].sort((a, b) => a[0].localeCompare(b[0])))
    for (const child of folder.dirs.values()) sortFolder(child)
  }
  sortFolder(root)
  return root
}

type Sel =
  | { kind: 'overview' }
  | { kind: 'agent'; name: string }
  | { kind: 'mcp'; name: string }
  | { kind: 'file'; path: string; label: string }
  | { kind: 'monitor' }

// AI App detail — a single Azure Function App identified as an AI App by the
// AZURE_FUNCTIONS_AGENTS_PROVIDER app setting. Shows its composition, endpoints,
// and source code, and lets the user deploy portal edits.
export default function AppDetailPage() {
  const { subscriptionId, app: appName } = useParams<{ subscriptionId: string; app: string }>()
  const navigate = useNavigate()
  const { selected, setSelected, identity } = useIdentity()
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
  // The selected agent's live metadata (trigger + whether it exposes a chat API),
  // used to organize the side panel and gate the "Try it" action.
  const selectedAgent =
    sel.kind === 'agent' && app ? app.agents.find((a) => a.name === sel.name) : undefined
  const codeFiles = app ? buildCodeFiles(app) : []
  const builtinCount = app?.agents.filter((a) => a.builtinEndpoints).length ?? 0
  const supportingCount = app?.supportingFunctions?.length ?? 0

  // Real file listing — merges the app's deployed package (via Kudu VFS or the
  // Flex zip's central directory) with any local drafts, so the sidebar shows
  // every file the "Deploy edits" step will push, not just a curated set.
  const qc = useQueryClient()
  const filesListQuery = useQuery({
    queryKey: ['sourceList', subForQuery, app?.resourceGroup ?? '', appName ?? ''],
    queryFn: () =>
      api.listSources({ subscription: subForQuery, app: app!.name, resourceGroup: app!.resourceGroup }),
    enabled: !!app,
    staleTime: 60_000,
    refetchOnMount: false,
  })
  const listedFiles: SourceListEntry[] = filesListQuery.data?.files ?? []

  // Union of the discovered files and the curated fallback list, so a fresh
  // app whose package isn't readable still surfaces the obvious well-known
  // files as editable draft slots.
  const tree = useMemo(() => buildFileTree(listedFiles, codeFiles), [listedFiles, codeFiles])

  // Delete a portal-side draft. Used by the file editor's Delete-draft button
  // and the sidebar's per-file action.
  const deleteDraft = useMutation({
    mutationFn: (relPath: string) =>
      api.deleteSourceDraft({ subscription: subForQuery, app: appName!, path: relPath }),
    onSuccess: (_res, relPath) => {
      qc.invalidateQueries({ queryKey: ['source', subForQuery, appName ?? '', relPath] })
      qc.invalidateQueries({ queryKey: ['sourceList', subForQuery, app?.resourceGroup ?? '', appName ?? ''] })
    },
  })

  // Create a new file draft. Prompts for a wwwroot-relative path, seeds an
  // empty draft, then selects it in the editor.
  const [newFileError, setNewFileError] = useState<string | null>(null)
  const addNewFile = async () => {
    if (!app) return
    setNewFileError(null)
    // eslint-disable-next-line no-alert
    const raw = window.prompt('New file path (wwwroot-relative, e.g. tools/hello.py)', 'tools/hello.py')
    if (!raw) return
    const path = raw.replace(/^\.?\/+/, '').trim()
    if (!path || path.includes('..')) {
      setNewFileError('Invalid path.')
      return
    }
    if (listedFiles.some((f) => f.path === path)) {
      setSel({ kind: 'file', path, label: path.split('/').pop() ?? path })
      return
    }
    try {
      await api.saveSource({ subscription: subForQuery, app: app.name, path, content: '' })
      await qc.invalidateQueries({ queryKey: ['sourceList', subForQuery, app.resourceGroup, app.name] })
      setSel({ kind: 'file', path, label: path.split('/').pop() ?? path })
    } catch (e) {
      setNewFileError((e as Error).message)
    }
  }

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

  // GitHub connection for this app — powers the contextual "Create PR" action that
  // appears in the editor toolbar once a saved (unpushed) draft exists.
  const { data: appConn } = useQuery({
    queryKey: ['githubAppConnection', subForQuery, app?.resourceGroup ?? '', appName ?? ''],
    queryFn: () =>
      api.githubAppConnection({ subscription: subForQuery, resourceGroup: app!.resourceGroup, app: app!.name }),
    enabled: !!app,
    staleTime: 30_000,
  })
  const [prBusy, setPrBusy] = useState(false)
  const [prPushed, setPrPushed] = useState(false)
  const [prResult, setPrResult] = useState<{ url: string; number?: number } | null>(null)
  const [prError, setPrError] = useState<string | null>(null)
  const createPr = async () => {
    if (!app || !appConn?.connected || !appConn.repoUrl) return
    const repo = appConn.repoUrl.replace('https://github.com/', '')
    setPrBusy(true)
    setPrError(null)
    try {
      const r = await api.githubConnect({
        subscription: subForQuery,
        resourceGroup: app.resourceGroup,
        app: app.name,
        mode: 'existing',
        repo,
        branch: appConn.branch || 'main',
      })
      setPrResult({ url: r.prUrl || r.htmlUrl, number: r.prNumber })
      setPrPushed(true)
    } catch (e) {
      setPrError((e as Error).message)
    } finally {
      setPrBusy(false)
    }
  }
  const onDraftSaved = () => {
    setPrPushed(false)
    setPrResult(null)
    setPrError(null)
  }
  const renderPrAction = ({ source, dirty }: { source: string; dirty: boolean }): ReactNode => {
    if (!appConn?.connected) return null
    if (prBusy) {
      return (
        <span className="muted" style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
          <span className="gh-spin" /> Opening pull request…
        </span>
      )
    }
    if (prPushed && prResult) {
      return (
        <a className="btn sm" href={prResult.url} target="_blank" rel="noreferrer" title="View the pull request">
          ✓ PR updated{prResult.number ? ` · #${prResult.number}` : ''} →
        </a>
      )
    }
    if (source === 'draft' && !dirty) {
      return (
        <>
          <Button
            appearance="primary"
            size="small"
            onClick={() => void createPr()}
            title="Open or update a pull request with your saved changes"
          >
            Create PR
          </Button>
          {prError && (
            <span className="muted" style={{ color: 'var(--red)', fontSize: 11 }}>
              {prError.slice(0, 90)}
            </span>
          )}
        </>
      )
    }
    return null
  }

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
            <Button
              appearance="primary"
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
            </Button>
            <Link className="btn" to="/create-agent">
              ＋ Add agent
            </Link>
            {app.agents.length > 0 && (
              <AddCapability
                variant="button"
                subscription={subForQuery}
                resourceGroup={app.resourceGroup}
                app={app.name}
                agentName={app.agents[0].name}
                agents={app.agents.map((a) => a.name)}
              />
            )}
            <a
              className="btn"
              href={buildFunctionAppMonitorUrl({
                tenantId: identity?.user?.tenantId,
                subscriptionId: subForQuery,
                resourceGroup: app.resourceGroup,
                app: app.name,
              })}
              target="_blank"
              rel="noreferrer"
              title="Open this Function App's Application Insights + invocation monitor in the Azure portal"
            >
              📊 Monitor
            </a>
            <a
              className="btn"
              href={buildFunctionAppFailuresUrl({
                tenantId: identity?.user?.tenantId,
                subscriptionId: subForQuery,
                resourceGroup: app.resourceGroup,
                app: app.name,
              })}
              target="_blank"
              rel="noreferrer"
              title="Open recent invocation failures (App Insights Failures blade)"
            >
              ⚠ Failures
            </a>
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
          <GitHubConnect
            github={{ subscription: subForQuery, resourceGroup: app.resourceGroup, app: app.name }}
            defaultCollapsed
          />

          <div className="components">
            <aside className="explorer">
              <button
                className={'node' + (sel.kind === 'overview' ? ' active' : '')}
                onClick={() => setSel({ kind: 'overview' })}
              >
                📊 Overview
              </button>
              <button
                className={'node' + (sel.kind === 'monitor' ? ' active' : '')}
                onClick={() => setSel({ kind: 'monitor' })}
                title="Invocations, latency, errors, and recent failures from App Insights"
              >
                📈 Monitor
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

              <div className="group-label" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <span>App files</span>
                <Button
                  size="small"
                  appearance="subtle"
                  onClick={() => void addNewFile()}
                  title="Create a new file in this app (saved as a draft)"
                >
                  ＋ New file
                </Button>
              </div>
              {filesListQuery.isLoading && (
                <div className="muted" style={{ fontSize: 12, padding: '4px 8px' }}>
                  Listing files…
                </div>
              )}
              {filesListQuery.error && (
                <div className="muted" style={{ fontSize: 12, padding: '4px 8px', color: 'var(--red)' }}>
                  Couldn’t list files: {(filesListQuery.error as Error).message.slice(0, 80)}
                </div>
              )}
              {newFileError && (
                <div className="muted" style={{ fontSize: 12, padding: '4px 8px', color: 'var(--red)' }}>
                  {newFileError}
                </div>
              )}
              <FileTreeView
                folder={tree}
                depth={0}
                sel={sel}
                onSelect={(file) => setSel({ kind: 'file', path: file.path, label: file.name })}
              />
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
                    <div style={{ display: 'inline-flex', gap: 8 }}>
                      {selectedAgent?.builtinEndpoints && (
                        <Link
                          className="btn sm"
                          to={`/playground/${enc(subForQuery)}/${enc(app.name)}/${enc(sel.name)}`}
                          title="Chat with this agent's built-in endpoint"
                        >
                          💬 Try it
                        </Link>
                      )}
                      <Link
                        className="btn sm"
                        to={`/agents/${enc(subForQuery)}/${enc(app.name)}/${enc(sel.name)}`}
                        title="Open the full agent page"
                      >
                        Open agent page →
                      </Link>
                    </div>
                  </div>
                  {selectedAgent && (
                    <p className="muted" style={{ fontSize: 12, marginTop: 0, marginBottom: 14 }}>
                      Runs as <span className="badge gray">{selectedAgent.trigger || 'http'}</span>
                      {selectedAgent.builtinEndpoints
                        ? ' · exposes a chat API you can try'
                        : ' · no chat endpoint (triggered agent)'}
                    </p>
                  )}

                  <AddCapability
                    subscription={subForQuery}
                    resourceGroup={app.resourceGroup}
                    app={app.name}
                    agentName={sel.name}
                  />

                  <span className="group-sub">Definition</span>
                  <p className="muted" style={{ fontSize: 12, margin: '4px 0 10px' }}>
                    Edit the agent’s <span className="mono">.agent.md</span> — its instructions, trigger, and
                    settings. Saves as a draft; publish with <strong>Deploy edits</strong>.
                  </p>
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
                    renderActions={renderPrAction}
                    onSaved={onDraftSaved}
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
                    renderActions={renderPrAction}
                    onSaved={onDraftSaved}
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
                    renderActions={({ source, dirty }) => {
                      const filePath = sel.kind === 'file' ? sel.path : ''
                      return (
                        <>
                          {renderPrAction({ source, dirty })}
                          {source === 'draft' && !dirty && filePath && (
                            <Button
                              size="small"
                              appearance="subtle"
                              disabled={deleteDraft.isPending}
                              onClick={() => deleteDraft.mutate(filePath)}
                              title="Discard this portal draft (does not touch the deployed file)"
                            >
                              {deleteDraft.isPending ? 'Deleting…' : 'Delete draft'}
                            </Button>
                          )}
                        </>
                      )
                    }}
                    onSaved={onDraftSaved}
                  />
                </>
              )}

              {sel.kind === 'monitor' && (
                <MonitorPane
                  subscription={subForQuery}
                  resourceGroup={app.resourceGroup}
                  app={app.name}
                  tenantId={identity?.user?.tenantId}
                />
              )}
            </section>
          </div>

          {endpoints.length > 0 && (
            <div className="card" style={{ marginTop: 18 }}>
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
        </>
      )}
    </>
  )
}

// Recursive sidebar view of the app's file tree. Renders folders as
// collapsible group headers and files as clickable rows; the currently
// selected file is highlighted. A small badge marks files that carry a
// portal-side draft (unpublished edits) so they stand out from deployed-only
// files at a glance.
function FileTreeView({
  folder,
  depth,
  sel,
  onSelect,
  prefix = '',
}: {
  folder: TreeFolder
  depth: number
  sel: Sel
  onSelect: (file: TreeFile) => void
  prefix?: string
}) {
  return (
    <>
      {folder.files.map((f) => {
        const active = sel.kind === 'file' && sel.path === f.path
        const draftBadge =
          f.source === 'draft' || f.source === 'both' ? (
            <span
              className="badge amber"
              style={{ marginLeft: 'auto', fontSize: 10 }}
              title={f.source === 'both' ? 'Deployed + draft' : 'Draft only'}
            >
              draft
            </span>
          ) : f.source === 'stub' ? (
            <span
              className="badge gray"
              style={{ marginLeft: 'auto', fontSize: 10 }}
              title="Not present in the deployed package — start a draft to create it"
            >
              stub
            </span>
          ) : null
        return (
          <button
            key={f.path}
            className={'node' + (active ? ' active' : '')}
            onClick={() => onSelect(f)}
            title={f.path}
            style={{ paddingLeft: 8 + depth * 12 }}
          >
            <span style={{ marginRight: 6 }}>{pickIcon(f.name)}</span>
            <span className="mono">{f.name}</span>
            {draftBadge}
          </button>
        )
      })}
      {[...folder.dirs.entries()].map(([dir, child]) => {
        const nextPrefix = prefix ? `${prefix}/${dir}` : dir
        return (
          <FileTreeFolder
            key={nextPrefix}
            name={dir}
            folder={child}
            depth={depth}
            sel={sel}
            onSelect={onSelect}
            prefix={nextPrefix}
          />
        )
      })}
    </>
  )
}

function FileTreeFolder({
  name,
  folder,
  depth,
  sel,
  onSelect,
  prefix,
}: {
  name: string
  folder: TreeFolder
  depth: number
  sel: Sel
  onSelect: (file: TreeFile) => void
  prefix: string
}) {
  // Expand top-level folders by default so common entry points (skills/,
  // tools/, agents/) are visible without a click; deeper folders start
  // collapsed to keep the sidebar scannable.
  const [open, setOpen] = useState(depth === 0)
  return (
    <>
      <button
        type="button"
        className="node"
        onClick={() => setOpen((v) => !v)}
        title={prefix}
        style={{ paddingLeft: 8 + depth * 12, fontWeight: 500 }}
      >
        <span style={{ marginRight: 6 }}>{open ? '📂' : '📁'}</span>
        <span className="mono">{name}</span>
      </button>
      {open && (
        <FileTreeView
          folder={folder}
          depth={depth + 1}
          sel={sel}
          onSelect={onSelect}
          prefix={prefix}
        />
      )}
    </>
  )
}

// Per-app monitoring pane — runs four curated KQL presets against the app's
// linked Application Insights component and renders the results as a compact
// stat row + a table of recent failures. Each failure row links out to the
// Transaction Search blade filtered by operation_Id, which is the fastest path
// from "this request failed" to "here is the full trace" without the portal
// having to fetch full stack traces itself.
function MonitorPane({
  subscription,
  resourceGroup,
  app,
  tenantId,
}: {
  subscription: string
  resourceGroup: string
  app: string
  tenantId?: string
}) {
  const [timeRange, setTimeRange] = useState<'1h' | '24h' | '7d'>('24h')
  const params = { subscription, resourceGroup, app, timeRange }
  const summary = useQuery({
    queryKey: ['ai:summary', subscription, resourceGroup, app, timeRange],
    queryFn: () => api.appInsightsQuery({ ...params, preset: 'summary' }),
    staleTime: 60_000,
    retry: false,
  })
  const agentsQ = useQuery({
    queryKey: ['ai:agents', subscription, resourceGroup, app, timeRange],
    queryFn: () => api.appInsightsQuery({ ...params, preset: 'agents' }),
    staleTime: 60_000,
    retry: false,
  })
  const failuresQ = useQuery({
    queryKey: ['ai:recentFailures', subscription, resourceGroup, app, timeRange],
    queryFn: () => api.appInsightsQuery({ ...params, preset: 'recentFailures' }),
    staleTime: 60_000,
    retry: false,
  })

  const err = (summary.error || agentsQ.error || failuresQ.error) as Error | undefined
  const summaryRow = rowsOf(summary.data)[0] ?? []
  const summaryCols = columnsOf(summary.data)
  const invocations = pick(summaryCols, summaryRow, 'invocations') as number | undefined
  const failures = pick(summaryCols, summaryRow, 'failures') as number | undefined
  const p95 = pick(summaryCols, summaryRow, 'p95_ms') as number | undefined
  const avg = pick(summaryCols, summaryRow, 'avg_ms') as number | undefined
  const errRate = invocations && invocations > 0 ? Math.round(((failures ?? 0) / invocations) * 1000) / 10 : 0

  const agentRows = rowsOf(agentsQ.data)
  const agentCols = columnsOf(agentsQ.data)
  const failureRows = rowsOf(failuresQ.data)
  const failureCols = columnsOf(failuresQ.data)

  const transactionUrl = (operationId: string) => {
    const componentId = summary.data?.componentId || agentsQ.data?.componentId || failuresQ.data?.componentId
    if (!componentId) return ''
    const tenant = tenantId ? `@${tenantId}` : ''
    return `https://portal.azure.com/#${tenant}/resource${componentId}/searchV1/searchTerm/${encodeURIComponent(operationId)}`
  }

  return (
    <>
      <div className="card-head">
        <h3 style={{ margin: 0 }}>Monitor</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          Live from App Insights · <span className="mono">cloud_RoleName == "{app}"</span>
        </span>
        <div style={{ flex: 1 }} />
        <div className="copy-as-tabs" role="tablist" aria-label="Time range">
          {(['1h', '24h', '7d'] as const).map((r) => (
            <button
              key={r}
              type="button"
              role="tab"
              aria-selected={r === timeRange}
              className={'copy-as-tab' + (r === timeRange ? ' is-active' : '')}
              onClick={() => setTimeRange(r)}
            >
              {r}
            </button>
          ))}
        </div>
      </div>

      {err && (
        <div className="note" style={{ marginTop: 10 }}>
          Couldn’t reach App Insights: {err.message}
        </div>
      )}

      <StatTiles
        items={[
          { n: invocations ?? 0, label: 'Invocations' },
          { n: failures ?? 0, label: 'Failures' },
          { n: `${errRate}%`, label: 'Error rate' },
          { n: p95 != null ? `${Math.round(p95)} ms` : '—', label: 'p95 latency' },
          { n: avg != null ? `${Math.round(avg)} ms` : '—', label: 'avg latency' },
        ]}
      />

      <div className="divider" />
      <span className="group-sub">Per-agent breakdown</span>
      {agentRows.length === 0 ? (
        <p className="muted" style={{ fontSize: 13 }}>
          {agentsQ.isLoading ? 'Loading…' : 'No invocations in this time range.'}
        </p>
      ) : (
        <div className="ai-table-wrap">
          <table className="ai-table">
            <thead>
              <tr>
                {agentCols.map((c) => (
                  <th key={c.name}>{prettyCol(c.name)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {agentRows.slice(0, 12).map((row, i) => (
                <tr key={i}>
                  {row.map((v, j) => (
                    <td key={j} className={agentCols[j]?.name === 'operation_Name' ? 'mono' : ''}>
                      {formatCell(agentCols[j]?.name, v)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="divider" />
      <span className="group-sub">Recent failures</span>
      {failureRows.length === 0 ? (
        <p className="muted" style={{ fontSize: 13 }}>
          {failuresQ.isLoading ? 'Loading…' : 'No failures in this time range.'}
        </p>
      ) : (
        <div className="ai-table-wrap">
          <table className="ai-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Operation</th>
                <th>Result</th>
                <th>Duration</th>
                <th>Session</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {failureRows.map((row, i) => {
                const operationId = String(pick(failureCols, row, 'operation_Id') ?? '')
                const link = operationId ? transactionUrl(operationId) : ''
                return (
                  <tr key={i}>
                    <td>{formatCell('timestamp', pick(failureCols, row, 'timestamp'))}</td>
                    <td className="mono">{String(pick(failureCols, row, 'name') ?? '')}</td>
                    <td>{String(pick(failureCols, row, 'resultCode') ?? '')}</td>
                    <td>{formatCell('duration', pick(failureCols, row, 'duration'))}</td>
                    <td className="mono">
                      {String(pick(failureCols, row, 'session') ?? '').slice(0, 8) || '—'}
                    </td>
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
    </>
  )
}

function columnsOf(result: { tables?: { columns: { name: string; type: string }[] }[] } | undefined) {
  return result?.tables?.[0]?.columns ?? []
}
function rowsOf(result: { tables?: { rows: unknown[][] }[] } | undefined) {
  return result?.tables?.[0]?.rows ?? []
}
function pick(columns: { name: string }[], row: unknown[], column: string): unknown {
  const i = columns.findIndex((c) => c.name === column)
  return i >= 0 ? row[i] : undefined
}
function prettyCol(name: string): string {
  return name
    .replace(/_/g, ' ')
    .replace(/\bMs\b/, 'ms')
    .replace(/^./, (c) => c.toUpperCase())
}
function formatCell(colName: string | undefined, value: unknown): string {
  if (value == null) return '—'
  if (colName === 'timestamp' && typeof value === 'string') {
    try {
      return new Date(value).toLocaleString()
    } catch {
      return value
    }
  }
  if (colName === 'duration' && typeof value === 'number') return `${Math.round(value)} ms`
  if ((colName === 'p95_ms' || colName === 'avg_ms') && typeof value === 'number') return `${Math.round(value)} ms`
  return String(value)
}
