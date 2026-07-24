import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, type LiveAgent, type LiveAgentApp } from '../api'
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

export default function AgentDetailPage() {
  const { subscriptionId, app: appName, name } = useParams<{
    subscriptionId: string
    app: string
    name: string
  }>()
  const navigate = useNavigate()
  const { selected, setSelected } = useIdentity()
  const [tab, setTab] = useState<'definition' | 'endpoints'>('definition')

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

          <div className="card">
            <div className="card-head">
              <div className="tabs">
                <button
                  className={'tab' + (tab === 'definition' ? ' active' : '')}
                  onClick={() => setTab('definition')}
                >
                  Definition (.agent.md)
                </button>
                <button
                  className={'tab' + (tab === 'endpoints' ? ' active' : '')}
                  onClick={() => setTab('endpoints')}
                  disabled={endpoints.length === 0}
                >
                  Endpoints
                </button>
              </div>
              <CopyButton
                text={tab === 'definition' ? markdown : endpointsText}
                title="Copy to clipboard"
              />
            </div>

            {tab === 'definition' ? (
              <pre className="code" aria-label="Agent definition">
                {markdown}
              </pre>
            ) : endpoints.length > 0 ? (
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
          </div>
        </>
      )}

      {isFetching && !scanning && <p className="cache-stamp">⟳ Refreshing…</p>}
    </>
  )
}
