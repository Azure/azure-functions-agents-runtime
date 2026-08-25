import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Button } from '@coreai/fluentui-react'
import { api, type LiveAgent, type LiveAgentApp } from '../api'
import { AddCapability } from '../components/AddCapability'
import { DraftEditor } from '../components/SourceEditor'
import { TriggerEditor } from '../components/TriggerEditor'
import { ObservabilityPanel } from '../components/ObservabilityPanel'
import { DeploymentStatus, GitHubConnect, useDeployJob } from '../deploy'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot, writeAgentsSnapshot } from '../query'

type DetailTab = 'instructions' | 'runs' | 'capabilities' | 'observability' | 'source'

interface McpServerSummary {
  name: string
  url: string
  tools: string[]
}

function buildAgentMarkdown(agent: LiveAgent): string {
  const trigger = agent.trigger || 'http'
  return [
    '---',
    `name: ${agent.name}`,
    `description: Discovered Hosted Skill ${agent.name}.`,
    ...(agent.provider ? [`provider: ${agent.provider}`] : []),
    ...(trigger === 'none' ? [] : ['trigger:', `  type: ${trigger.endsWith('_trigger') ? trigger : `${trigger}_trigger`}`, '  args: {}']),
    `builtin_endpoints: ${agent.builtinEndpoints ? 'true' : 'false'}`,
    '---',
    '',
    'Add instructions for this Hosted Skill.',
  ].join('\n')
}

function agentFrontMatter(content: string): string {
  const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/)
  return match?.[1].trim() ?? ''
}

function configuredAgentName(content: string, fallback: string): string {
  const match = agentFrontMatter(content).match(/^name:\s*(.+?)\s*$/m)
  return match?.[1].trim().replace(/^(['"])(.*)\1$/, '$2') || fallback
}

function parseMcpServers(content?: string | null): McpServerSummary[] | null {
  if (!content) return []
  try {
    const parsed = JSON.parse(content) as {
      servers?: Record<string, { url?: string; tools?: string[] }>
    }
    return Object.entries(parsed.servers ?? {}).map(([name, server]) => ({
      name,
      url: server.url ?? '',
      tools: server.tools ?? [],
    }))
  } catch {
    return null
  }
}

function triggerLabel(trigger: string): string {
  const value = trigger || 'http'
  if (value.includes('http')) return 'Chat or API'
  if (value.includes('timer')) return 'Schedule'
  if (value.includes('connector') || value.includes('generic')) return 'Connected service'
  if (value.includes('queue') || value.includes('blob') || value.includes('event') || value.includes('service_bus')) {
    return 'Data event'
  }
  const normalized = value.replace(/_trigger$/, '').replace(/_/g, ' ')
  return normalized.replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function endpointUrls(agent: LiveAgent): { label: string; value: string }[] {
  if (!agent.defaultHostName) return []
  const base = `https://${agent.defaultHostName}`
  const urls = (agent.routes ?? []).map((route) => ({
    label: 'HTTP trigger',
    value: `${base}/${route.replace(/^\//, '')}`,
  }))
  if (agent.builtinEndpoints) {
    const name = encodeURIComponent(agent.name)
    urls.unshift(
      { label: 'Chat', value: `${base}/agents/${name}/` },
      { label: 'Chat API', value: `${base}/agents/${name}/chat` },
    )
  }
  return urls
}

export default function SkillDetailPage() {
  const { subscriptionId, app: appName, name } = useParams<{
    subscriptionId: string
    app: string
    name: string
  }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const { selected, setSelected } = useIdentity()
  const deployJob = useDeployJob()

  useEffect(() => {
    if (subscriptionId && subscriptionId !== selected) setSelected(subscriptionId)
  }, [subscriptionId, selected, setSelected])

  const subForQuery = subscriptionId || selected
  const snapshot = useMemo(() => readAgentsSnapshot(subForQuery), [subForQuery])
  const { data, error: queryError, isFetching, dataUpdatedAt } = useQuery({
    queryKey: queryKeys.liveAgents(subForQuery),
    queryFn: () => api.liveAgents(subForQuery),
    enabled: !!subForQuery,
    staleTime: Infinity,
    refetchOnMount: false,
    initialData: snapshot?.data,
    initialDataUpdatedAt: snapshot?.updatedAt,
  })

  useEffect(() => {
    if (subForQuery && data) writeAgentsSnapshot(subForQuery, data, dataUpdatedAt)
  }, [subForQuery, data, dataUpdatedAt])

  const agent = useMemo(
    () => data?.agents.find((candidate) => candidate.app === appName && candidate.name === name),
    [data, appName, name],
  )
  const hostApp: LiveAgentApp | undefined = useMemo(
    () => data?.apps.find((candidate) => candidate.name === appName),
    [data, appName],
  )
  const backTo = `/agents/${subscriptionId ?? selected}`
  const error = queryError ? (queryError as Error).message : null
  const scanning = !!subForQuery && !data && !error
  const requestedTab = searchParams.get('tab') as DetailTab | null
  const tab: DetailTab = ['instructions', 'runs', 'observability', 'source'].includes(requestedTab ?? '')
    ? requestedTab!
    : 'instructions'
  const setTab = (next: DetailTab) => setSearchParams(next === 'instructions' ? {} : { tab: next })

  const definitionKey = ['agentDefinition', subForQuery, agent?.app ?? '', agent?.name ?? '']
  const fallback = agent ? buildAgentMarkdown(agent) : ''
  const { data: definition, isLoading: definitionLoading, error: definitionError } = useQuery({
    queryKey: definitionKey,
    queryFn: () => api.getAgentDefinition({
      subscription: subForQuery,
      app: agent!.app,
      resourceGroup: agent!.resourceGroup,
      name: agent!.name,
    }),
    enabled: !!agent,
    staleTime: Infinity,
    refetchOnMount: false,
  })
  const { data: sourceList } = useQuery({
    queryKey: ['sourceList', subForQuery, agent?.resourceGroup ?? '', agent?.app ?? ''],
    queryFn: () => api.listSources({ subscription: subForQuery, app: agent!.app, resourceGroup: agent!.resourceGroup }),
    enabled: !!agent,
    staleTime: 60_000,
    refetchOnMount: false,
  })
  const { data: mcpSource } = useQuery({
    queryKey: ['source', subForQuery, agent?.app ?? '', 'mcp.json'],
    queryFn: () => api.getSource({ subscription: subForQuery, app: agent!.app, resourceGroup: agent!.resourceGroup, path: 'mcp.json' }),
    enabled: !!agent,
    staleTime: Infinity,
    refetchOnMount: false,
  })

  const parsedMcpServers = parseMcpServers(mcpSource?.content)
  const mcpServers = parsedMcpServers ?? []
  const mcpSourceInvalid = parsedMcpServers === null
  const sourcePaths = sourceList?.files.map((file) => file.path) ?? []
  const pythonTools = sourcePaths.filter((path) => /^tools\/.+\.py$/i.test(path) && !path.endsWith('__init__.py'))
  const knowledge = sourcePaths.filter((path) => /^skills\/[^/]+\/SKILL\.md$/i.test(path))
  const appFunctions = hostApp?.supportingFunctions ?? []
  const urls = agent ? endpointUrls(agent) : []
  const hasDraft = definition?.source === 'draft'
  const hasDeployableEdits = hasDraft || !!sourceList?.files.some((file) => file.source === 'draft' || file.source === 'both')
  const capabilityCount = mcpServers.length + pythonTools.length + knowledge.length + appFunctions.length
  const definitionMetadata = agentFrontMatter(definition?.content || fallback)

  const { data: appConnection } = useQuery({
    queryKey: ['githubAppConnection', subForQuery, agent?.resourceGroup ?? '', agent?.app ?? ''],
    queryFn: () => api.githubAppConnection({ subscription: subForQuery, resourceGroup: agent!.resourceGroup, app: agent!.app }),
    enabled: !!agent,
    staleTime: 30_000,
  })
  const [prBusy, setPrBusy] = useState(false)
  const [prResult, setPrResult] = useState<{ url: string; number?: number } | null>(null)
  const [prError, setPrError] = useState<string | null>(null)
  const createPr = async () => {
    if (!agent || !appConnection?.connected || !appConnection.repoUrl) return
    setPrBusy(true)
    setPrError(null)
    try {
      const result = await api.githubConnect({
        subscription: subForQuery,
        resourceGroup: agent.resourceGroup,
        app: agent.app,
        mode: 'existing',
        repo: appConnection.repoUrl.replace('https://github.com/', ''),
        branch: appConnection.branch || 'main',
      })
      setPrResult({ url: result.prUrl || result.htmlUrl, number: result.prNumber })
    } catch (caught) {
      setPrError((caught as Error).message)
    } finally {
      setPrBusy(false)
    }
  }
  const renderPrAction = ({ source, dirty }: { source: string; dirty: boolean }): ReactNode => {
    if (!appConnection?.connected || source !== 'draft' || dirty) return null
    if (prResult) return <a className="btn sm" href={prResult.url} target="_blank" rel="noreferrer">View PR{prResult.number ? ` #${prResult.number}` : ''} ↗</a>
    return <Button appearance="primary" size="small" disabled={prBusy} onClick={() => void createPr()}>{prBusy ? 'Opening PR…' : 'Create PR'}</Button>
  }

  if (data && !agent && !scanning) {
    return <div className="empty">Hosted Skill <strong>{name}</strong> was not found. <Link to={backTo}>Return to Hosted Skills</Link>.</div>
  }

  return (
    <>
      {!agent && <div className="breadcrumb">Home / <Link to={backTo}>Hosted Skills</Link> / {name}</div>}
      {scanning && <p className="page-sub">Scanning subscription…</p>}
      {error && <p className="page-sub">Failed to load Hosted Skill: {error}</p>}
      {agent && (
        <>
          <div className="skill-draft-bar">
            <div className="breadcrumb skill-bar-breadcrumb">Home / <Link to={backTo}>Hosted Skills</Link> / {name}</div>
            <div className="skill-draft-actions">
              <span className="skill-deploy-state">
                <span className={'draft-dot' + (hasDeployableEdits ? ' active' : '')} />
                <strong>{hasDeployableEdits ? 'Draft changes' : 'Deployed'}</strong>
              </span>
              {agent.builtinEndpoints && <Link className="btn sm" to={`/playground/${subForQuery}/${encodeURIComponent(agent.app)}/${encodeURIComponent(agent.name)}`}>Test</Link>}
              <Button size="small" appearance="primary" disabled={!hasDeployableEdits || deployJob.phase === 'running'} onClick={() => deployJob.redeploy({ subscription: subForQuery, resourceGroup: agent.resourceGroup, app: agent.app })}>
                {deployJob.phase === 'running' ? 'Deploying…' : 'Deploy'}
              </Button>
            </div>
          </div>

          <div className="skill-title-row">
            <div>
              <div className="page-title"><h1>{agent.name}</h1></div>
            </div>
          </div>

          {hostApp && hostApp.agents.length > 1 && (
            <nav className="skill-file-switcher" aria-label={`Hosted Skills in ${hostApp.name}`}>
              <span>Hosted Skills in this app</span>
              <div>
                {hostApp.agents.map((hostedSkill) => {
                  const active = hostedSkill.name === agent.name
                  const query = tab === 'instructions' ? '' : `?tab=${tab}`
                  return (
                    <Link
                      key={hostedSkill.name}
                      className={'skill-file-link' + (active ? ' active' : '')}
                      aria-current={active ? 'page' : undefined}
                      to={`/agents/${encodeURIComponent(subForQuery)}/${encodeURIComponent(agent.app)}/${encodeURIComponent(hostedSkill.name)}${query}`}
                    >
                      {hostedSkill.name}.agent.md
                    </Link>
                  )
                })}
              </div>
            </nav>
          )}

          <nav className="skill-subtabs" aria-label="Hosted Skill sections">
            <button aria-current={tab === 'instructions' ? 'page' : undefined} className={'skill-subtab' + (tab === 'instructions' ? ' active' : '')} onClick={() => setTab('instructions')}>Instructions</button>
            <button aria-current={tab === 'runs' ? 'page' : undefined} className={'skill-subtab' + (tab === 'runs' ? ' active' : '')} onClick={() => setTab('runs')}>How it runs</button>
            <button className="skill-subtab" disabled>What it can use <span className="badge gray">Coming soon</span></button>
            <button aria-current={tab === 'observability' ? 'page' : undefined} className={'skill-subtab' + (tab === 'observability' ? ' active' : '')} onClick={() => setTab('observability')}>Observability</button>
            <button aria-current={tab === 'source' ? 'page' : undefined} className={'skill-subtab' + (tab === 'source' ? ' active' : '')} onClick={() => setTab('source')}>Source &amp; GitHub</button>
            {agent.builtinEndpoints && <Link className="skill-subtab" to={`/playground/${subForQuery}/${encodeURIComponent(agent.app)}/${encodeURIComponent(agent.name)}`}>Test</Link>}
          </nav>

          <DeploymentStatus phase={deployJob.phase} result={deployJob.result} portalUrl={deployJob.portalUrl} message={deployJob.message} />

          {tab === 'instructions' && (
            <div className="skill-editor-layout">
              <section className="skill-instructions-panel">
                <div className="agent-instructions-editor">
                  <DraftEditor
                    queryKey={definitionKey}
                    load={() => api.getAgentDefinition({ subscription: subForQuery, app: agent.app, resourceGroup: agent.resourceGroup, name: agent.name })}
                    save={(content) => api.saveAgentDefinition({ subscription: subForQuery, app: agent.app, name: agent.name, content })}
                    fallback={fallback}
                    toolbarLead={(
                      <div className="skill-instructions-head">
                        <h2>Instructions</h2>
                        <strong className="mono">{agent.name}.agent.md</strong>
                      </div>
                    )}
                    beforeEditor={(
                      <div className="agent-file-frame">
                        <details className="agent-file-config">
                          <summary>YAML configuration</summary>
                          <pre>{definitionMetadata || 'No YAML front matter found.'}</pre>
                        </details>
                      </div>
                    )}
                    mode="instructions"
                    ariaLabel="Hosted Skill instructions"
                    renderActions={renderPrAction}
                    onSaved={() => { setPrResult(null); setPrError(null) }}
                    validationKind="agent.md"
                  />
                </div>
                {prError && <p className="muted" style={{ color: 'var(--red)' }}>{prError}</p>}
              </section>
              <aside className="skill-aside-stack">
                <div className="skill-aside-card"><h3>How it runs</h3><strong>{triggerLabel(agent.trigger)}</strong>{urls[0] && <span className="mono">{urls[0].value.replace(/^https?:\/\/[^/]+/, '')}</span>}<button className="link-button" onClick={() => setTab('runs')}>Change</button></div>
                <div className="skill-aside-card"><h3>Inherited configuration</h3><dl><div><dt>Provider</dt><dd>{agent.provider || 'App default'}</dd></div><div><dt>Region</dt><dd>{agent.region || hostApp?.location || '—'}</dd></div><div><dt>Endpoints</dt><dd>{agent.builtinEndpoints ? 'Enabled' : 'Disabled'}</dd></div></dl></div>
                <div className="skill-aside-card"><h3>What it can use</h3><strong>{capabilityCount} discovered</strong><span className="muted">MCP servers, tools, knowledge, and app functions</span><span className="badge gray">Coming soon</span></div>
                <div className="skill-aside-card"><h3>Full source</h3><span className="muted">View or edit the complete file, including YAML front matter.</span><button className="link-button" onClick={() => setTab('source')}>Open agent code</button></div>
              </aside>
            </div>
          )}

          {tab === 'runs' && (
            <section>
              <div className="skill-section-head"><div><h2>How it runs</h2><p>A Hosted Skill has one primary trigger. Save changes as a draft, then deploy the Function App to make them live.</p></div></div>
              {definitionLoading && <p className="muted">Loading trigger definition…</p>}
              {definitionError && <div className="gh-err">Couldn’t load the complete agent definition: {(definitionError as Error).message}</div>}
              {!definitionLoading && !definitionError && definition?.content && (
                <TriggerEditor
                  subscription={subForQuery}
                  app={agent.app}
                  agentName={agent.name}
                  content={definition.content}
                  source={definition.source}
                  queryKey={definitionKey}
                />
              )}
              {!definitionLoading && !definitionError && !definition?.content && (
                <div className="note warn">The complete <span className="mono">.agent.md</span> source is unavailable. Open Source &amp; GitHub or grant storage access before changing its trigger.</div>
              )}
            </section>
          )}

          {tab === 'observability' && (
            <ObservabilityPanel
              subscription={subForQuery}
              resourceGroup={agent.resourceGroup}
              app={agent.app}
              agentName={configuredAgentName(definition?.content || fallback, agent.name)}
            />
          )}

          {tab === 'capabilities' && (
            <section>
              <div className="skill-section-head"><div><h2>What it can use</h2><p>App-owned resources this Hosted Skill can call. Add an MCP server, tool, knowledge module, or connector trigger.</p></div><AddCapability variant="button" scope="capabilities" buttonLabel="Add MCP, tool, or connector" subscription={subForQuery} resourceGroup={agent.resourceGroup} app={agent.app} agentName={agent.name} /></div>
              <div className="capability-groups">
                {mcpSourceInvalid && <div className="note warn"><strong>mcp.json needs attention.</strong><br />The file is not valid JSON. Open Source &amp; GitHub to repair it before deployment.</div>}
                <CapabilityGroup title="MCP servers" empty="No MCP servers configured." items={mcpServers.map((server) => ({ name: server.name, path: server.url || 'mcp.json', detail: server.tools.length ? server.tools.join(' · ') : 'All advertised tools' }))} />
                <CapabilityGroup title="Python tools" empty="No Python tools discovered." items={pythonTools.map((path) => ({ name: path.split('/').pop()?.replace(/\.py$/, '') || path, path, detail: 'App-shared tool module' }))} />
                <CapabilityGroup title="Knowledge modules" empty="No knowledge modules discovered." items={knowledge.map((path) => ({ name: path.split('/')[1] || path, path, detail: 'Reusable Markdown guidance' }))} />
                <CapabilityGroup title="App functions" empty="No supporting functions discovered." items={appFunctions.map((fn) => ({ name: fn.name, path: 'function_app.py', detail: `${triggerLabel(fn.trigger)} trigger` }))} />
              </div>
              <div className="note" style={{ marginTop: 16 }}><strong>App-owned, skill-selected.</strong><br />New MCP servers, tools, and connector definitions are saved as drafts. Review their source before deploying the Function App.</div>
            </section>
          )}

          {tab === 'source' && (
            <section className="source-github-stack">
              <div className="skill-section-head"><div><h2>Source &amp; GitHub</h2><p>Move the current Function App source into a repository, then use pull requests for future Hosted Skill changes.</p></div>{appConnection?.connected && <span className="badge green">Repository connected</span>}</div>
              <div className="github-migration-steps"><div><span>1</span><strong>Connect a repository</strong><p>Create a new repository or select an existing one.</p></div><div><span>2</span><strong>Open the initial PR</strong><p>The portal exports the app source and saved drafts to a branch.</p></div><div><span>3</span><strong>Deploy from GitHub</strong><p>Enable GitHub Actions after review so merged code becomes the source of truth.</p></div></div>
              <GitHubConnect github={{ subscription: subForQuery, resourceGroup: agent.resourceGroup, app: agent.app }} />
              <details className="advanced-source-card" open>
                <summary>Agent code · {agent.name}.agent.md</summary>
                <div className="advanced-source-body">
                  <DraftEditor
                    queryKey={definitionKey}
                    load={() => api.getAgentDefinition({ subscription: subForQuery, app: agent.app, resourceGroup: agent.resourceGroup, name: agent.name })}
                    save={(content) => api.saveAgentDefinition({ subscription: subForQuery, app: agent.app, name: agent.name, content })}
                    fallback={fallback}
                    renderActions={renderPrAction}
                    validationKind="agent.md"
                  />
                </div>
              </details>
            </section>
          )}
        </>
      )}
      {isFetching && !scanning && <p className="cache-stamp">Refreshing…</p>}
    </>
  )
}

function CapabilityGroup({ title, empty, items }: { title: string; empty: string; items: { name: string; path: string; detail: string }[] }) {
  return (
    <div className="card capability-group">
      <div className="card-head"><div><h3>{title}</h3><span className="muted">{items.length} discovered</span></div></div>
      {items.length ? items.map((item) => <div className="capability-row" key={`${title}:${item.path}:${item.name}`}><div><strong>{item.name}</strong><span className="mono muted">{item.path}</span></div><span className="muted">{item.detail}</span><span className="badge green">Available</span></div>) : <p className="muted">{empty}</p>}
    </div>
  )
}