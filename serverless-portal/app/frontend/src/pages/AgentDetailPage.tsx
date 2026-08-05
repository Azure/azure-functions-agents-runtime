import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type LiveAgent, type LiveAgentApp } from '../api'
import { useDeployJob, DeploymentStatus, GitHubConnect } from '../deploy'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot, writeAgentsSnapshot } from '../query'

// Reconstruct the `.agent.md` frontmatter from what live Azure discovery can
// see. The instructions body lives in the source project (or the portal's blob
// working copy) and is not retrievable from ARM, so it is called out explicitly.
function buildAgentMarkdown(agent: LiveAgent): string {
  const lines = [
    '---',
    `name: ${agent.name}`,
    ...(agent.provider ? ['# model provider (AZURE_FUNCTIONS_AGENTS_PROVIDER app setting)', `provider: ${agent.provider}`] : []),
    `trigger: ${agent.trigger || 'http'}`,
    ...(agent.routes?.length ? [`# http route(s): ${agent.routes.join(', ')}`] : []),
    `builtin_endpoints: ${agent.builtinEndpoints ? 'true' : 'false'}`,
    '---',
    '',
    '# Instructions',
    '',
    '# The instructions body is authored in the source `*.agent.md` file and is',
    '# not exposed by live Azure discovery. Connect the source repo or the blob',
    '# working copy to view and edit the full definition here.',
  ]
  return lines.join('\n')
}

// The runtime registers built-in endpoints under a stable naming convention
// (registration/endpoints.py): chat UI at `agents/<name>/`, REST chat at
// `agents/<name>/chat`, streaming at `.../chatstream`, and MCP at the shared
// `/runtime/webhooks/mcp` webhook. Custom-trigger agents instead expose their
// own HTTP route(s); non-HTTP triggers (timer, queue, Service Bus, connector…)
// have no callable URL and are surfaced as the trigger type only.
function buildEndpoints(agent: LiveAgent): { label: string; url: string; kind: string }[] {
  const host = agent.defaultHostName
  if (!host) return []
  const base = `https://${host}`
  const out: { label: string; url: string; kind: string }[] = []
  if (agent.builtinEndpoints) {
    const name = encodeURIComponent(agent.name)
    out.push({ label: 'Chat UI', url: `${base}/agents/${name}/`, kind: 'GET' })
    out.push({ label: 'Chat API', url: `${base}/agents/${name}/chat`, kind: 'POST' })
    out.push({ label: 'Chat stream (SSE)', url: `${base}/agents/${name}/chatstream`, kind: 'POST' })
    out.push({ label: 'MCP', url: `${base}/runtime/webhooks/mcp`, kind: 'POST' })
  }
  for (const route of agent.routes ?? []) {
    out.push({
      label: 'HTTP trigger',
      url: `${base}/${String(route).replace(/^\//, '')}`,
      kind: 'POST',
    })
  }
  return out
}

function CopyButton({ text, title }: { text: string; title: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className="btn sm"
      title={title}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
          setCopied(true)
          setTimeout(() => setCopied(false), 1200)
        } catch {
          /* clipboard unavailable */
        }
      }}
    >
      {copied ? '✓ Copied' : '⧉ Copy'}
    </button>
  )
}

// Loads a deployed source file (an `.agent.md` or app code) or a saved portal
// draft, lets the user edit it, and saves edits to the portal working copy.
// Publishing a draft to the live app is a separate step that isn't wired yet.
function DraftEditor({
  queryKey,
  load,
  save,
  fallback,
}: {
  queryKey: unknown[]
  load: () => Promise<{ content: string; source: string }>
  save: (content: string) => Promise<unknown>
  fallback: string
}) {
  const qc = useQueryClient()
  const { data, isLoading, error } = useQuery({
    queryKey,
    queryFn: load,
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnReconnect: false,
  })

  // Reset local edits whenever a fresh copy arrives (initial load, and after a
  // save invalidates + refetches).
  const [text, setText] = useState<string | null>(null)
  useEffect(() => {
    setText(null)
  }, [data])

  const saveMutation = useMutation({
    mutationFn: (content: string) => save(content),
    onSuccess: () => qc.invalidateQueries({ queryKey }),
  })

  if (isLoading) return <p className="muted">Loading…</p>
  if (error) return <p className="muted">Couldn’t load: {(error as Error).message}</p>

  const base = data?.content || fallback
  const value = text ?? base
  const dirty = value !== base
  const source = data?.source ?? 'none'
  const unreadable = !data?.content

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        {source === 'draft' ? (
          <span className="badge amber">
            <span className="dot" /> Draft (unpublished)
          </span>
        ) : source === 'deployed' ? (
          <span className="badge green">
            <span className="dot" /> Deployed source
          </span>
        ) : (
          <span className="badge gray">Source not readable</span>
        )}
        {dirty && (
          <span className="muted" style={{ fontSize: 12 }}>
            · unsaved changes
          </span>
        )}
        {!dirty && saveMutation.isSuccess && (
          <span className="muted" style={{ fontSize: 12 }}>
            · saved
          </span>
        )}
        <div style={{ flex: 1 }} />
        <button
          className="btn sm"
          onClick={() => setText(null)}
          disabled={!dirty || saveMutation.isPending}
          title="Discard unsaved changes"
        >
          Reset
        </button>
        <button
          className="btn sm primary"
          onClick={() => saveMutation.mutate(value)}
          disabled={!dirty || saveMutation.isPending}
        >
          {saveMutation.isPending ? 'Saving…' : 'Save draft'}
        </button>
      </div>
      {unreadable && (
        <p className="muted" style={{ fontSize: 12, margin: '0 0 8px' }}>
          The deployed source couldn’t be read (permission or plan) — start from here; saving stores a
          portal draft.
        </p>
      )}
      <textarea
        className="editor"
        spellCheck={false}
        value={value}
        onChange={(e) => setText(e.target.value)}
        aria-label="Source editor"
      />
      {saveMutation.isError && (
        <p className="muted" style={{ color: 'var(--red)', fontSize: 12 }}>
          Save failed: {(saveMutation.error as Error).message}
        </p>
      )}
      <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
        Edits are saved to a portal-side working copy. Use <strong>Deploy edits</strong> above to publish
        this app with your saved changes.
      </p>
    </div>
  )
}

export default function AgentDetailPage() {
  const { subscriptionId, app: appName, name } = useParams<{
    subscriptionId: string
    app: string
    name: string
  }>()
  const navigate = useNavigate()
  const { selected, setSelected } = useIdentity()
  type Sel = { kind: 'agent' } | { kind: 'source'; path: string; label: string } | { kind: 'endpoints' }
  const [sel, setSel] = useState<Sel>({ kind: 'agent' })
  const deployJob = useDeployJob()

  // Deeplink → state: adopt the subscription from the URL so a shared/reloaded
  // detail link restores the exact view even before identity has loaded.
  useEffect(() => {
    if (subscriptionId && subscriptionId !== selected) {
      setSelected(subscriptionId)
    }
  }, [subscriptionId, selected, setSelected])

  const subForQuery = subscriptionId || selected
  const snapshot = useMemo(() => readAgentsSnapshot(subForQuery), [subForQuery])

  // Reuse the exact same cached discovery the list page populates, so opening a
  // deeplink hydrates instantly from localStorage and only scans on a cold load.
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

  const agent: LiveAgent | undefined = useMemo(
    () => data?.agents.find((a) => a.app === appName && a.name === name),
    [data, appName, name],
  )
  const hostApp: LiveAgentApp | undefined = useMemo(
    () => data?.apps.find((a) => a.name === appName),
    [data, appName],
  )

  const error = queryError ? (queryError as Error).message : null
  const scanning = !!subForQuery && !data && !error
  const backTo = `/agents/${subscriptionId ?? selected}`

  const markdown = agent ? buildAgentMarkdown(agent) : ''
  const endpoints = agent ? buildEndpoints(agent) : []
  const endpointsText = endpoints.map((e) => `${e.kind.padEnd(5)} ${e.url}`).join('\n')

  return (
    <>
      <div className="breadcrumb">
        Home / <Link to={backTo}>Agents</Link> / {name}
      </div>
      <div className="page-title">
        <button className="btn ghost sm" onClick={() => navigate(backTo)} title="Back to agents">
          ← Back
        </button>
        <h1>{name}</h1>
        {agent && (
          <span className="badge blue" style={{ marginLeft: 4 }}>
            {agent.trigger || 'http'}
          </span>
        )}
      </div>

      {scanning && <p className="page-sub">Scanning subscription…</p>}
      {error && <p className="page-sub">Failed to load agent: {error}</p>}
      {data && !agent && !scanning && (
        <div className="empty">
          Agent <strong>{name}</strong> was not found in <strong>{appName}</strong>.{' '}
          <Link to={backTo}>Return to the agent list</Link>.
        </div>
      )}

      {agent && (
        <>
          <p className="page-sub">
            Serverless agent hosted in Function App <span className="mono">{agent.app}</span>
            {agent.defaultHostName && (
              <>
                {' — '}
                <a
                  href={`https://${agent.defaultHostName}/agents/${encodeURIComponent(agent.name)}/`}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open chat →
                </a>
              </>
            )}
          </p>

          <div className="grid cols-2" style={{ marginBottom: 18 }}>
            <div className="card">
              <h3>Deployment</h3>
              <dl className="meta-grid">
                <dt>Function App</dt>
                <dd className="mono">{agent.app}</dd>
                <dt>Resource group</dt>
                <dd>{agent.resourceGroup || '—'}</dd>
                <dt>Region</dt>
                <dd>{agent.region || hostApp?.location || '—'}</dd>
                <dt>Host name</dt>
                <dd className="mono">{agent.defaultHostName || '—'}</dd>
              </dl>
            </div>
            <div className="card">
              <h3>Configuration</h3>
              <dl className="meta-grid">
                <dt>Provider</dt>
                <dd>
                  {agent.provider ? (
                    <span className="badge gray">{agent.provider}</span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </dd>
                <dt>Trigger</dt>
                <dd>
                  <span className="badge blue">{agent.trigger || 'http'}</span>
                </dd>
                <dt>Built-in endpoints</dt>
                <dd>
                  {agent.builtinEndpoints ? (
                    <span className="badge green">
                      <span className="dot" /> enabled
                    </span>
                  ) : (
                    <span className="muted">disabled</span>
                  )}
                </dd>
                <dt>Agents in app</dt>
                <dd>{hostApp?.agents.length ?? 1}</dd>
              </dl>
            </div>
          </div>

          <div className="toolbar" style={{ marginBottom: 12 }}>
            <button
              className="btn primary"
              disabled={deployJob.phase === 'running'}
              onClick={() =>
                deployJob.redeploy({
                  subscription: subForQuery,
                  resourceGroup: agent.resourceGroup,
                  app: agent.app,
                })
              }
            >
              {deployJob.phase === 'running' ? 'Deploying…' : '🚀 Deploy edits'}
            </button>
            {agent.builtinEndpoints && (
              <Link
                className="btn"
                to={`/playground/${subForQuery}/${encodeURIComponent(agent.app)}/${encodeURIComponent(agent.name)}`}
              >
                💬 Open in Playground
              </Link>
            )}
            <span className="muted" style={{ fontSize: 12 }}>
              Redeploys <span className="mono">{agent.app}</span> from its current source with your saved
              drafts applied.
            </span>
          </div>
          <DeploymentStatus
            phase={deployJob.phase}
            result={deployJob.result}
            portalUrl={deployJob.portalUrl}
            message={deployJob.message}
          />
          <GitHubConnect
            github={{ subscription: subForQuery, resourceGroup: agent.resourceGroup, app: agent.app }}
          />

          <div className="components">
            <aside className="explorer">
              <div className="group-label">Agent</div>
              <button
                className={'node' + (sel.kind === 'agent' ? ' active' : '')}
                onClick={() => setSel({ kind: 'agent' })}
              >
                📄 <span className="mono">{agent.name}.agent.md</span>
              </button>

              {hostApp && hostApp.supportingFunctions && hostApp.supportingFunctions.length > 0 && (
                <>
                  <div className="group-label">Supporting functions</div>
                  {hostApp.supportingFunctions.map((fn) => (
                    <button
                      key={fn.name}
                      className={
                        'node' + (sel.kind === 'source' && sel.label === fn.name ? ' active' : '')
                      }
                      onClick={() =>
                        setSel({ kind: 'source', path: 'function_app.py', label: fn.name })
                      }
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
              <button
                className={
                  'node' +
                  (sel.kind === 'source' && sel.label === 'function_app.py' ? ' active' : '')
                }
                onClick={() =>
                  setSel({ kind: 'source', path: 'function_app.py', label: 'function_app.py' })
                }
              >
                🐍 <span className="mono">function_app.py</span>
              </button>

              {endpoints.length > 0 && (
                <>
                  <div className="group-label">Endpoints</div>
                  <button
                    className={'node' + (sel.kind === 'endpoints' ? ' active' : '')}
                    onClick={() => setSel({ kind: 'endpoints' })}
                  >
                    🔗 Endpoints
                    <span className="badge gray" style={{ marginLeft: 'auto' }}>
                      {endpoints.length}
                    </span>
                  </button>
                </>
              )}
            </aside>

            <section className="component-editor">
              {sel.kind === 'agent' && (
                <>
                  <div className="card-head">
                    <h3 className="mono" style={{ margin: 0 }}>
                      {agent.name}.agent.md
                    </h3>
                    <span className="badge blue">agent</span>
                  </div>
                  <DraftEditor
                    key="agent"
                    queryKey={['agentDefinition', subForQuery, agent.app, agent.name]}
                    load={() =>
                      api.getAgentDefinition({
                        subscription: subForQuery,
                        app: agent.app,
                        resourceGroup: agent.resourceGroup,
                        name: agent.name,
                      })
                    }
                    save={(content) =>
                      api.saveAgentDefinition({
                        subscription: subForQuery,
                        app: agent.app,
                        name: agent.name,
                        content,
                      })
                    }
                    fallback={markdown}
                  />
                </>
              )}

              {sel.kind === 'source' && (
                <>
                  <div className="card-head">
                    <h3 className="mono" style={{ margin: 0 }}>
                      {sel.path}
                    </h3>
                    {sel.label !== sel.path && (
                      <span className="muted" style={{ fontSize: 12 }}>
                        <span className="mono">{sel.label}</span> is defined here
                      </span>
                    )}
                  </div>
                  <DraftEditor
                    key={'source:' + sel.path}
                    queryKey={['source', subForQuery, agent.app, sel.path]}
                    load={() =>
                      api.getSource({
                        subscription: subForQuery,
                        app: agent.app,
                        resourceGroup: agent.resourceGroup,
                        path: sel.path,
                      })
                    }
                    save={(content) =>
                      api.saveSource({
                        subscription: subForQuery,
                        app: agent.app,
                        path: sel.path,
                        content,
                      })
                    }
                    fallback=""
                  />
                </>
              )}

              {sel.kind === 'endpoints' && (
                <>
                  <div className="card-head">
                    <h3 style={{ margin: 0 }}>Endpoints</h3>
                    {endpoints.length > 0 && (
                      <CopyButton text={endpointsText} title="Copy all URLs" />
                    )}
                  </div>
                  {endpoints.length > 0 ? (
                    <div className="endpoint-list">
                      {endpoints.map((e) => (
                        <div className="endpoint-row" key={e.url}>
                          <span className={'badge ' + (e.kind === 'GET' ? 'gray' : 'purple')}>
                            {e.kind}
                          </span>
                          <span className="cell-title">{e.label}</span>
                          <code className="endpoint-url">{e.url}</code>
                          <CopyButton text={e.url} title={`Copy ${e.label} URL`} />
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="muted">This agent does not expose built-in HTTP endpoints.</p>
                  )}
                </>
              )}
            </section>
          </div>
        </>
      )}

      {isFetching && !scanning && <p className="cache-stamp">⟳ Refreshing…</p>}
    </>
  )
}
