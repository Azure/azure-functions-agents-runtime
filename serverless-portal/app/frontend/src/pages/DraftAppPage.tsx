import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type LiveAgent, type LiveAgentApp, type LiveDiscovery, type CapabilitySuggestion } from '../api'
import { useDeployJob, DeploymentStatus } from '../deploy'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot, writeAgentsSnapshot } from '../query'
import { DeployTargetPicker, SearchableSelect, Icon } from '../components/ui'
import { Button, Input } from '@coreai/fluentui-react'
import { DraftEditor } from '../components/SourceEditor'
import { AddCapability } from '../components/AddCapability'
import { skillSlug } from '../capabilities'
import {
  type Draft,
  loadDraft,
  saveDraft,
  clearDraft,
  slugify,
  composeAgentMd,
  defaultAppName,
  defaultResourceGroup,
  sanitizeAppName,
  FLEX_REGIONS,
} from '../agentDraft'

// Review the app generated on the Create page, then deploy it to a Function App
// to try it — and, once deployed, connect a GitHub repo. The generated agent
// lives in sessionStorage; deploying provisions the app and (only after that)
// GitHub connect + the Foundry access grant appear inline via DeploymentStatus.
export default function DraftAppPage() {
  const navigate = useNavigate()
  const { selected, subscriptions, identity } = useIdentity()
  const [draft, setDraft] = useState<Draft>(loadDraft)

  // Auto-save the draft for this session on every change.
  useEffect(() => {
    saveDraft(draft)
  }, [draft])

  // Suggest resource names once so the user doesn't have to type them. Only
  // fills blanks, so an edited name survives reloads.
  useEffect(() => {
    setDraft((d) => {
      const appName = d.newApp.appName || defaultAppName(d.name)
      const resourceGroup =
        d.newApp.rgMode === 'new' && !d.newApp.resourceGroup
          ? defaultResourceGroup(d.name)
          : d.newApp.resourceGroup
      if (appName === d.newApp.appName && resourceGroup === d.newApp.resourceGroup) return d
      return { ...d, newApp: { ...d.newApp, appName, resourceGroup } }
    })
  }, [])

  // Function App name availability (names share the global *.azurewebsites.net namespace).
  const [nameStatus, setNameStatus] = useState<'idle' | 'checking' | 'available' | 'taken' | 'error'>('idle')
  const [nameMsg, setNameMsg] = useState('')

  // Capabilities: once the user adds MCP tools / triggers, the agent is saved as a
  // backend draft (so the shared AddCapability flow can edit it) and the app name is
  // locked so those drafts stay attached to it.
  const [capsEnabled, setCapsEnabled] = useState(false)
  const [materializing, setMaterializing] = useState(false)
  const [capErr, setCapErr] = useState('')

  const qc = useQueryClient()
  const cachedRef = useRef(false)

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }))
  const targetSub = draft.targetSubscription || selected

  const snapshot = useMemo(() => readAgentsSnapshot(targetSub), [targetSub])
  const { data, isFetching: appsLoading } = useQuery({
    queryKey: queryKeys.liveAgents(targetSub),
    queryFn: () => api.liveAgents(targetSub),
    enabled: !!targetSub,
    staleTime: Infinity,
    refetchOnMount: false,
    initialData: snapshot?.data,
    initialDataUpdatedAt: snapshot?.updatedAt,
  })
  const apps = data?.apps ?? []

  const { data: rgData, isFetching: rgLoading } = useQuery({
    queryKey: ['resource-groups', targetSub],
    queryFn: () => api.listResourceGroups(targetSub),
    enabled: !!targetSub && draft.target === 'new',
    staleTime: 5 * 60 * 1000,
  })
  const resourceGroups = rgData?.resourceGroups ?? []

  const selectTargetSub = (sub: string) =>
    setDraft((d) => ({
      ...d,
      targetSubscription: sub,
      existingApp: '',
      newApp: { ...d.newApp, resourceGroup: '' },
    }))

  const slug = slugify(draft.name)
  const fileName = `${slug}.agent.md`
  const composed = composeAgentMd(draft)
  const previewMd = draft.mdOverride ?? composed

  const deployJob = useDeployJob()
  const deploying = deployJob.phase === 'running'
  const deployedAppName = draft.target === 'existing' ? draft.existingApp : draft.newApp.appName
  const deployedRg =
    draft.target === 'existing'
      ? (apps.find((a) => a.name === draft.existingApp)?.resourceGroup ?? '')
      : draft.newApp.resourceGroup

  // The app the capability drafts attach to (the new app being created).
  const capApp = sanitizeAppName(draft.newApp.appName).replace(/-+$/g, '')
  const capRg = draft.newApp.resourceGroup

  // Save the generated agent as a backend draft so the shared AddCapability flow
  // (triggers, MCP tools, skills) can read and edit it. Locks the app name.
  const enableCapabilities = async () => {
    if (!capApp || !draft.name.trim()) return
    setMaterializing(true)
    setCapErr('')
    try {
      await api.saveAgentDefinition({ subscription: targetSub, app: capApp, name: draft.name, content: previewMd })
      setCapsEnabled(true)
    } catch (e) {
      setCapErr((e as Error).message)
    } finally {
      setMaterializing(false)
    }
  }

  // Phase A: infer capabilities from the prompt (skill-grounded) once enabled.
  const foundryForGen = {
    resourceGroup: draft.foundryResourceGroup,
    account: draft.foundryAccount,
    openaiEndpoint: draft.foundryOpenaiEndpoint,
    model: draft.foundryModel,
  }
  // Debounce the description used for capability planning so typing doesn't
  // spin up a new query on every keystroke and flicker the results panel.
  const [debouncedDescription, setDebouncedDescription] = useState(draft.description)
  useEffect(() => {
    const handle = setTimeout(() => setDebouncedDescription(draft.description), 400)
    return () => clearTimeout(handle)
  }, [draft.description])

  const { data: planData, isFetching: planning } = useQuery({
    queryKey: ['plan-capabilities', targetSub, draft.name, draft.foundryModel, debouncedDescription],
    queryFn: () =>
      api.planCapabilities({ subscription: targetSub, description: debouncedDescription, foundry: foundryForGen }),
    enabled: capsEnabled && !!draft.foundryAccount && !!draft.foundryOpenaiEndpoint && !!debouncedDescription.trim(),
    staleTime: Infinity,
    refetchOnMount: false,
  })
  const suggestions = planData?.capabilities ?? []
  const [genState, setGenState] = useState<Record<string, 'busy' | 'done' | 'error'>>({})

  const kindLabel = (kind: string) =>
    kind === 'custom_tool'
      ? 'Tool'
      : kind === 'mcp'
        ? 'MCP'
        : kind === 'skill'
          ? 'Skill'
          : kind === 'connector_trigger'
            ? 'Connector'
            : 'Trigger'

  // One-click generate for the additive code kinds (tool + skill). Triggers and
  // MCP servers are added via the panel below (triggers replace the agent md;
  // MCP is config, not generated code).
  const generateSuggestion = async (s: CapabilitySuggestion) => {
    setGenState((m) => ({ ...m, [s.name]: 'busy' }))
    try {
      if (s.kind === 'custom_tool') {
        const r = await api.generateCapability({
          subscription: targetSub, app: capApp, kind: 'custom_tool',
          name: s.name, description: s.description, groundInSkills: true, foundry: foundryForGen,
        })
        const slug = s.name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'tool'
        await api.saveSource({ subscription: targetSub, app: capApp, path: `tools/${slug}.py`, content: r.content })
      } else {
        const r = await api.generateCapability({
          subscription: targetSub, app: capApp, kind: 'skill',
          name: s.name, description: s.description, groundInSkills: true, foundry: foundryForGen,
        })
        await api.saveSource({ subscription: targetSub, app: capApp, path: `skills/${skillSlug(s.name)}/SKILL.md`, content: r.content })
      }
      setGenState((m) => ({ ...m, [s.name]: 'done' }))
    } catch {
      setGenState((m) => ({ ...m, [s.name]: 'error' }))
    }
  }

  const runDeploy = () => {
    const target =
      draft.target === 'existing'
        ? {
            kind: 'existing' as const,
            app: draft.existingApp,
            resourceGroup: apps.find((a) => a.name === draft.existingApp)?.resourceGroup ?? '',
          }
        : {
            kind: 'new' as const,
            appName: sanitizeAppName(draft.newApp.appName).replace(/-+$/g, ''),
            resourceGroup: draft.newApp.resourceGroup,
            region: draft.newApp.region,
            foundryEndpoint: draft.foundryEndpoint,
            foundryModel: draft.foundryModel,
            // In pick mode we know the Foundry account, so the backend can
            // auto-grant the new app's identity access to it.
            ...(draft.foundryMode === 'pick' && draft.foundryAccount
              ? {
                  foundryAccount: {
                    subscription: draft.foundrySubscription || selected,
                    resourceGroup: draft.foundryResourceGroup,
                    account: draft.foundryAccount,
                  },
                }
              : {}),
          }
    deployJob.deploy({ subscription: targetSub, agent: { fileName, content: previewMd }, target })
  }

  const nameValid = draft.name.trim().length > 0
  const targetValid =
    draft.target === 'existing'
      ? !!draft.existingApp
      : !!draft.newApp.appName && !!draft.newApp.resourceGroup && !!draft.newApp.region
  const foundryValid = !!draft.foundryModel && (draft.target === 'existing' || !!draft.foundryEndpoint)
  const canDeploy = nameValid && targetValid && foundryValid && !!selected && deployJob.phase !== 'running'

  // When a new app finishes deploying, optimistically add it to the discovery
  // cache + snapshot so it shows on the dashboard without a manual Hard refresh.
  useEffect(() => {
    if (deployJob.phase !== 'deployed' || cachedRef.current || draft.target !== 'new' || !capApp) return
    cachedRef.current = true
    const host = (deployJob.result?.url ?? '').replace(/^https?:\/\//, '')
    const agentInApp = {
      name: draft.name,
      trigger: draft.trigger,
      builtinEndpoints: draft.builtinEndpoints,
      routes: [] as string[],
      supportingFunctions: [] as string[],
    }
    const appEntry: LiveAgentApp = {
      name: capApp,
      resourceGroup: draft.newApp.resourceGroup,
      location: draft.newApp.region,
      provider: draft.provider || 'foundry',
      defaultHostName: host,
      agents: [agentInApp],
      supportingFunctions: [],
    }
    const agentEntry: LiveAgent = {
      ...agentInApp,
      app: capApp,
      resourceGroup: draft.newApp.resourceGroup,
      region: draft.newApp.region,
      provider: draft.provider || 'foundry',
      defaultHostName: host,
    }
    const key = queryKeys.liveAgents(targetSub)
    const prev =
      qc.getQueryData<LiveDiscovery>(key) ??
      readAgentsSnapshot(targetSub)?.data ?? { subscriptionId: targetSub, apps: [], agents: [] }
    const next: LiveDiscovery = {
      ...prev,
      subscriptionId: targetSub,
      apps: [appEntry, ...prev.apps.filter((a) => a.name !== capApp)],
      agents: [agentEntry, ...prev.agents.filter((a) => !(a.app === capApp && a.name === draft.name))],
    }
    qc.setQueryData(key, next)
    writeAgentsSnapshot(targetSub, next, Date.now())
  }, [deployJob.phase, deployJob.result, draft, capApp, targetSub, qc])

  // Reset the availability result whenever the app name changes.
  useEffect(() => {
    setNameStatus('idle')
    setNameMsg('')
  }, [draft.newApp.appName])

  const checkName = async () => {
    const name = draft.newApp.appName.trim()
    if (!name) return
    setNameStatus('checking')
    setNameMsg('')
    try {
      const r = await api.checkName({ subscription: targetSub, name })
      setNameStatus(r.available ? 'available' : 'taken')
      if (!r.available) setNameMsg(r.message || 'That name is already taken.')
    } catch (e) {
      setNameStatus('error')
      setNameMsg((e as Error).message)
    }
  }

  const startOver = () => {
    clearDraft()
    navigate('/create-agent')
  }

  // Arrived without a generated agent (e.g. a refresh after the tab was closed)
  // — guide the user back to describe one.
  const hasContent = !!draft.instructions.trim() || draft.mdOverride != null
  if (!hasContent) {
    return (
      <>
        <div className="breadcrumb">
          Home / <Link to={`/agents/${selected}`}>AI Apps</Link> / New app
        </div>
        <div className="page-title">
          <h1>No generated app yet</h1>
        </div>
        <div className="empty">
          Describe an agent to generate its code first. <Link to="/create-agent">Create an AI App →</Link>
        </div>
      </>
    )
  }

  return (
    <>
      <div className="breadcrumb">
        Home / <Link to={`/agents/${selected}`}>AI Apps</Link> / <Link to="/create-agent">Create</Link> / Review
      </div>
      <div className="page-title">
        <Button appearance="subtle" size="small" onClick={() => navigate('/create-agent')} title="Back to describe">
          ← Back
        </Button>
        <h1 className="mono">{fileName}</h1>
        <span className="badge blue">
          <span className="dot" /> {draft.foundryModel || 'no model'}
        </span>
      </div>
      <p className="page-sub">Review, add capabilities, and deploy. Kept only in this browser session.</p>

      {deploying && (
        <div className="note" style={{ marginBottom: 12 }}>
          <strong>Deploying…</strong> This can take a few minutes — editing is locked until it finishes.
          <div className="deploy-shimmer" style={{ marginTop: 10, marginBottom: 0 }} />
        </div>
      )}

      <div className="grid cols-2" style={{ alignItems: 'start' }}>
        <div
          className={deploying ? 'deploying-lock' : undefined}
          style={{ display: 'flex', flexDirection: 'column', gap: 16 }}
        >
          <div className="card">
            <h3>Agent</h3>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Name</label>
              <Input
                type="text"
                value={draft.name}
                placeholder="support-triage"
                disabled={capsEnabled}
                onChange={(_, data) => set('name', data.value)}
              />
              <div className="hint">
                File <span className="mono">{fileName}</span> and route slug.
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h3 className="mono" style={{ margin: 0 }}>
                {fileName}
              </h3>
              {capsEnabled ? (
                <span className="badge blue">draft</span>
              ) : draft.mdOverride != null ? (
                <Button
                  appearance="subtle"
                  size="small"
                  onClick={() => set('mdOverride', null)}
                  title="Recompose from the generated instructions"
                >
                  ↺ Reset
                </Button>
              ) : (
                <span className="badge gray">generated</span>
              )}
            </div>
            {capsEnabled ? (
              <DraftEditor
                key={`agent:${capApp}:${draft.name}`}
                queryKey={['agentDefinition', targetSub, capApp, draft.name]}
                load={() =>
                  api.getAgentDefinition({
                    subscription: targetSub,
                    app: capApp,
                    resourceGroup: capRg,
                    name: draft.name,
                  })
                }
                save={(content) =>
                  api.saveAgentDefinition({ subscription: targetSub, app: capApp, name: draft.name, content })
                }
                fallback={previewMd}
              />
            ) : (
              <textarea
                className="editor"
                spellCheck={false}
                value={previewMd}
                onChange={(e) => set('mdOverride', e.target.value)}
                aria-label="Agent definition"
              />
            )}
          </div>

          {draft.target === 'new' &&
            (!capsEnabled ? (
              <div className="card">
                <div className="card-head">
                  <h3 style={{ margin: 0 }}>Capabilities</h3>
                </div>
                <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
                  Add MCP tools, triggers, connectors, or skills — generated with your Foundry model and
                  shipped when you deploy. Enabling locks the app name.
                </p>
                <button
                  className="btn"
                  disabled={materializing || !capApp || !draft.name.trim()}
                  onClick={() => void enableCapabilities()}
                >
                  {materializing ? (
                    'Preparing…'
                  ) : (
                    <>
                      <Icon name="plus" size={14} /> Add MCP tools & triggers
                    </>
                  )}
                </button>
                {capErr && (
                  <p className="muted" style={{ color: 'var(--red)', fontSize: 12, marginTop: 8 }}>
                    {capErr}
                  </p>
                )}
              </div>
            ) : (
              <>
                <div className="card">
                  <div className="card-head">
                    <h3 style={{ margin: 0 }}>Suggested from your prompt</h3>
                    {planning && (
                      <span className="muted" style={{ fontSize: 12 }}>Analyzing…</span>
                    )}
                  </div>
                  {!planning && suggestions.length === 0 ? (
                    <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                      No specific capabilities detected — add any you need below.
                    </p>
                  ) : (
                    <div className="pill-row">
                      {suggestions.map((s) => {
                        const st = genState[s.name]
                        const canGen = s.kind === 'custom_tool' || s.kind === 'skill'
                        return (
                          <div key={s.name} className="suggestion" title={s.description}>
                            <span className="badge gray">{kindLabel(s.kind)}</span>
                            <span className="mono">{s.name}</span>
                            {canGen ? (
                              st === 'done' ? (
                                <span className="badge green">
                                  <span className="dot" /> added
                                </span>
                              ) : (
                                <button
                                  className="btn sm"
                                  disabled={st === 'busy'}
                                  onClick={() => void generateSuggestion(s)}
                                >
                                  {st === 'busy' ? (
                                    'Generating…'
                                  ) : (
                                    <>
                                      <Icon name="sparkles" size={13} /> Generate
                                    </>
                                  )}
                                </button>
                              )
                            ) : (
                              <span className="muted" style={{ fontSize: 11 }}>add below</span>
                            )}
                            {st === 'error' && (
                              <span className="muted" style={{ color: 'var(--red)', fontSize: 11 }}>failed</span>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
                <AddCapability
                  subscription={targetSub}
                  resourceGroup={capRg}
                  app={capApp}
                  agentName={draft.name}
                />
              </>
            ))}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div className={'card' + (deploying ? ' deploying-lock' : '')}>
            <h3>Deploy to try it</h3>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              Choose where to run it. Creating a new app provisions an Azure Functions Flex Consumption app
              and reuses your Foundry model.
            </p>
            <div className="field">
              <label>Subscription</label>
              <SearchableSelect
                value={targetSub}
                onChange={selectTargetSub}
                options={subscriptions.map((s) => ({ value: s.id, label: s.name }))}
                placeholder="Select a subscription…"
                ariaLabel="Subscription"
              />
            </div>
            <DeployTargetPicker
              value={{ mode: draft.target, existingApp: draft.existingApp, newApp: draft.newApp }}
              onChange={(patch) =>
                setDraft((d) => ({
                  ...d,
                  ...(patch.mode !== undefined ? { target: patch.mode } : {}),
                  ...(patch.existingApp !== undefined ? { existingApp: patch.existingApp } : {}),
                }))
              }
              onNewApp={(patch) =>
                setDraft((d) => ({
                  ...d,
                  newApp: {
                    ...d.newApp,
                    ...patch,
                    ...(patch.appName !== undefined ? { appName: sanitizeAppName(patch.appName) } : {}),
                  },
                }))
              }
              apps={apps}
              appsLoading={appsLoading}
              resourceGroups={resourceGroups}
              rgLoading={rgLoading}
              regions={FLEX_REGIONS}
              modelHint={draft.foundryModel}
              lockAppName={capsEnabled}
            />
            {draft.target === 'new' && (
              <div className="toolbar" style={{ marginTop: 10 }}>
                <button
                  className="btn sm"
                  onClick={() => void checkName()}
                  disabled={!draft.newApp.appName.trim() || nameStatus === 'checking'}
                >
                  {nameStatus === 'checking' ? 'Checking…' : 'Check name availability'}
                </button>
                {nameStatus === 'available' && (
                  <span className="badge green">
                    <span className="dot" /> Available
                  </span>
                )}
                {nameStatus === 'taken' && (
                  <span className="muted" style={{ color: 'var(--red)', fontSize: 12 }}>
                    {nameMsg || 'Taken, try another name'}
                  </span>
                )}
                {nameStatus === 'error' && (
                  <span className="muted" style={{ fontSize: 12 }}>Couldn't check right now</span>
                )}
              </div>
            )}
            <div className="toolbar" style={{ marginTop: 14 }}>
              <button className="btn primary" disabled={!canDeploy} onClick={runDeploy}>
                {deployJob.phase === 'running' ? (
                  'Deploying…'
                ) : (
                  <>
                    <Icon name="rocket" size={14} />{' '}
                    {draft.target === 'new' ? 'Create app & deploy' : 'Deploy to app'}
                  </>
                )}
              </button>
              <button className="btn" onClick={startOver}>
                Start over
              </button>
              {!foundryValid && (
                <span className="muted" style={{ fontSize: 12 }}>
                  Set a Foundry project on the Model step (← Back) to fill the endpoint.
                </span>
              )}
              {foundryValid && !nameValid && (
                <span className="muted" style={{ fontSize: 12 }}>Enter an agent name.</span>
              )}
              {foundryValid && nameValid && !targetValid && (
                <span className="muted" style={{ fontSize: 12 }}>Choose or configure a Function App.</span>
              )}
            </div>
            <p className="muted" style={{ fontSize: 12, marginTop: 10, marginBottom: 0 }}>
              <Icon name="github" size={13} style={{ verticalAlign: '-2px' }} /> Connect a GitHub repo after
              deploying.
            </p>
          </div>

          <DeploymentStatus
            phase={deployJob.phase}
            result={deployJob.result}
            portalUrl={deployJob.portalUrl}
            message={deployJob.message}
            grant={
              draft.foundryMode === 'pick' && draft.foundryAccount
                ? {
                    subscription: draft.foundrySubscription || selected,
                    resourceGroup: draft.foundryResourceGroup,
                    account: draft.foundryAccount,
                    tenantId: identity?.user?.tenantId,
                  }
                : undefined
            }
            github={
              deployedAppName && deployedRg
                ? { subscription: targetSub, resourceGroup: deployedRg, app: deployedAppName }
                : undefined
            }
          />

          {deployJob.phase === 'deployed' && deployedAppName && (
            <div className="card">
              <div className="card-head">
                <h3 style={{ margin: 0 }}>✓ Deployed — what's next?</h3>
              </div>
              <p className="muted" style={{ fontSize: 13, marginTop: 0, marginBottom: 12 }}>
                Three small things will take you from a running app to a demo-able flow.
              </p>
              <ol className="next-steps">
                <li>
                  <span className="next-step-num">1</span>
                  <div>
                    <div className="next-step-title">Test in the playground</div>
                    <div className="muted" style={{ fontSize: 12 }}>
                      Send a real prompt through the built-in chat endpoint. Tools + MCP calls stream into
                      the trace panel so you can verify wiring.
                    </div>
                    <Link
                      className="btn sm"
                      style={{ marginTop: 6 }}
                      to={`/playground/${encodeURIComponent(targetSub)}/${encodeURIComponent(deployedAppName)}`}
                    >
                      Open Playground →
                    </Link>
                  </div>
                </li>
                <li>
                  <span className="next-step-num">2</span>
                  <div>
                    <div className="next-step-title">Connect GitHub for CI/CD</div>
                    <div className="muted" style={{ fontSize: 12 }}>
                      Push the app's source to a repo so a `git push` re-deploys it — no more portal round-trips
                      once the workflow is set up.
                    </div>
                    <Link
                      className="btn sm"
                      style={{ marginTop: 6 }}
                      to={`/apps/${encodeURIComponent(targetSub)}/${encodeURIComponent(deployedAppName)}#github`}
                    >
                      Connect GitHub →
                    </Link>
                  </div>
                </li>
                <li>
                  <span className="next-step-num">3</span>
                  <div>
                    <div className="next-step-title">Add a tool, MCP server, or skill</div>
                    <div className="muted" style={{ fontSize: 12 }}>
                      Give the agent an ability — a Python <code>@tool</code>, a remote MCP endpoint, or a
                      Markdown skill. Every change saves as a draft; <strong>Deploy edits</strong> pushes them.
                    </div>
                    <Link
                      className="btn sm"
                      style={{ marginTop: 6 }}
                      to={`/apps/${encodeURIComponent(targetSub)}/${encodeURIComponent(deployedAppName)}`}
                    >
                      Open the app →
                    </Link>
                  </div>
                </li>
              </ol>
            </div>
          )}
        </div>
      </div>
    </>
  )
}
