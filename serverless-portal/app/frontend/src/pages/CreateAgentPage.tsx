import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { useDeployJob, DeploymentStatus } from '../deploy'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot } from '../query'

// Regions that support Azure Functions Flex Consumption (+ the default Foundry
// gpt-5.4 Global Standard deployment) — matches the repo's infra allow-list.
const FLEX_REGIONS = [
  'brazilsouth',
  'canadacentral',
  'canadaeast',
  'centralus',
  'eastus',
  'eastus2',
  'northcentralus',
  'southcentralus',
  'westus',
  'westus3',
]

type Trigger = 'http' | 'timer' | 'connector'

const TEMPLATES: Record<
  string,
  { label: string; builtin: boolean; sandbox: boolean; trigger: Trigger; instructions: string }
> = {
  chat: {
    label: 'Chat assistant',
    builtin: true,
    sandbox: false,
    trigger: 'http',
    instructions: 'You are a helpful assistant. Answer the user clearly and concisely.',
  },
  'http-task': {
    label: 'HTTP-triggered task',
    builtin: false,
    sandbox: false,
    trigger: 'http',
    instructions:
      'You are an HTTP-triggered agent. Read the request body, perform the task, and return a concise result.',
  },
  scheduled: {
    label: 'Scheduled job',
    builtin: false,
    sandbox: false,
    trigger: 'timer',
    instructions: 'You run on a schedule. Perform the periodic task and log a short summary.',
  },
  blank: { label: 'Blank', builtin: false, sandbox: false, trigger: 'http', instructions: '' },
}

interface NewApp {
  rgMode: 'existing' | 'new'
  resourceGroup: string
  region: string
  appName: string
}

interface Draft {
  name: string
  description: string
  template: string
  provider: string
  // Foundry model (required): reuse an existing deployment or create one in AI Foundry.
  foundrySubscription: string
  foundryMode: 'pick' | 'manual'
  foundryAccount: string
  foundryResourceGroup: string
  foundryOpenaiEndpoint: string
  foundryEndpoint: string
  foundryModel: string
  builtinEndpoints: boolean
  sandbox: boolean
  trigger: Trigger
  instructions: string
  mdOverride: string | null
  targetSubscription: string
  target: 'existing' | 'new'
  existingApp: string
  newApp: NewApp
}

const DEFAULT_DRAFT: Draft = {
  name: '',
  description: '',
  template: 'chat',
  provider: 'foundry',
  foundrySubscription: '',
  foundryMode: 'pick',
  foundryAccount: '',
  foundryResourceGroup: '',
  foundryOpenaiEndpoint: '',
  foundryEndpoint: '',
  foundryModel: '',
  builtinEndpoints: true,
  sandbox: false,
  trigger: 'http',
  instructions: TEMPLATES.chat.instructions,
  mdOverride: null,
  targetSubscription: '',
  target: 'existing',
  existingApp: '',
  newApp: { rgMode: 'new', resourceGroup: '', region: 'westus3', appName: '' },
}

const DRAFT_KEY = 'create-agent-draft'

// Ephemeral: the in-progress agent lives in sessionStorage, so it survives
// reloads/navigation within the tab but is discarded when the browser closes.
function loadDraft(): Draft {
  try {
    const raw = sessionStorage.getItem(DRAFT_KEY)
    if (raw) return { ...DEFAULT_DRAFT, ...JSON.parse(raw) }
  } catch {
    /* ignore malformed draft */
  }
  return DEFAULT_DRAFT
}

function slugify(name: string): string {
  const s = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
  return s || 'agent'
}

function composeAgentMd(d: Draft): string {
  const slug = slugify(d.name)
  const lines: string[] = ['---', `name: ${d.name || slug}`]
  if (d.description) lines.push(`description: ${d.description}`)
  if (d.foundryModel) lines.push(`model: ${d.foundryModel}`)
  if (d.trigger === 'http') {
    lines.push('trigger:', '  type: http_trigger', '  args:', `    route: ${slug}`, '    methods: ["POST"]')
  } else if (d.trigger === 'timer') {
    lines.push('trigger:', '  type: timer_trigger', '  args:', '    schedule: "0 0 */6 * * *"')
  } else if (d.trigger === 'connector') {
    lines.push('trigger:', '  type: connector_trigger', '  args: {}')
  }
  lines.push(`builtin_endpoints: ${d.builtinEndpoints ? 'true' : 'false'}`)
  if (d.sandbox) lines.push('system_tools:', '  dynamic_sessions_code_interpreter: true')
  lines.push('---', '', d.instructions || '')
  return lines.join('\n')
}

export default function CreateAgentPage() {
  const navigate = useNavigate()
  const { selected, subscriptions, identity } = useIdentity()
  const [draft, setDraft] = useState<Draft>(loadDraft)

  // Persist to sessionStorage on every change (auto-save for the session).
  useEffect(() => {
    try {
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft))
    } catch {
      /* storage full/unavailable — non-fatal */
    }
  }, [draft])

  const foundrySub = draft.foundrySubscription || selected
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

  const {
    data: foundryData,
    isFetching: foundryLoading,
    refetch: refetchFoundry,
  } = useQuery({
    queryKey: ['foundry', foundrySub],
    queryFn: () => api.listFoundry(foundrySub),
    enabled: !!foundrySub,
    staleTime: 5 * 60 * 1000,
  })
  const foundryAccounts = foundryData?.accounts ?? []
  const selectedAccount = foundryAccounts.find((a) => a.name === draft.foundryAccount)

  const { data: rgData, isFetching: rgLoading } = useQuery({
    queryKey: ['resource-groups', targetSub],
    queryFn: () => api.listResourceGroups(targetSub),
    enabled: !!targetSub && draft.target === 'new',
    staleTime: 5 * 60 * 1000,
  })
  const resourceGroups = rgData?.resourceGroups ?? []

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }))
  const setNewApp = <K extends keyof NewApp>(key: K, value: NewApp[K]) =>
    setDraft((d) => ({ ...d, newApp: { ...d.newApp, [key]: value } }))

  // Switch the Foundry subscription → clear the picked account/model.
  const selectFoundrySub = (sub: string) =>
    setDraft((d) => ({
      ...d,
      foundrySubscription: sub,
      foundryAccount: '',
      foundryResourceGroup: '',
      foundryOpenaiEndpoint: '',
      foundryEndpoint: '',
      foundryModel: '',
    }))

  // Switch the target subscription → clear the picked app / resource group.
  const selectTargetSub = (sub: string) =>
    setDraft((d) => ({
      ...d,
      targetSubscription: sub,
      existingApp: '',
      newApp: { ...d.newApp, resourceGroup: '' },
    }))

  // Pick a Foundry account → seed its rg/endpoint, and auto-select a lone project/model.
  const selectAccount = (name: string) => {
    const acc = foundryAccounts.find((a) => a.name === name)
    setDraft((d) => ({
      ...d,
      foundryAccount: name,
      foundryResourceGroup: acc?.resourceGroup ?? '',
      foundryOpenaiEndpoint: acc?.openaiEndpoint ?? '',
      foundryEndpoint: acc && acc.projects.length === 1 ? acc.projects[0].endpoint : '',
      foundryModel: acc && acc.models.length === 1 ? acc.models[0].deployment : '',
    }))
  }

  // Manual entry clears the picker-derived account fields (which the AI generator
  // needs), so ✨ Generate is only offered when a model is actually selected.
  const setFoundryMode = (mode: 'pick' | 'manual') =>
    setDraft((d) =>
      mode === 'manual'
        ? { ...d, foundryMode: mode, foundryAccount: '', foundryResourceGroup: '', foundryOpenaiEndpoint: '' }
        : { ...d, foundryMode: mode },
    )

  const [generating, setGenerating] = useState(false)
  const [genError, setGenError] = useState<string | null>(null)
  const [step, setStep] = useState<1 | 2>(draft.foundryModel ? 2 : 1)
  const canGenerate =
    !!draft.foundryAccount && !!draft.foundryOpenaiEndpoint && !!draft.description.trim() && !generating
  const generate = async () => {
    if (!canGenerate) return
    setGenerating(true)
    setGenError(null)
    try {
      const r = await api.generateAgentMd({
        subscription: foundrySub,
        name: draft.name,
        description: draft.description,
        foundry: {
          resourceGroup: draft.foundryResourceGroup,
          account: draft.foundryAccount,
          openaiEndpoint: draft.foundryOpenaiEndpoint,
          model: draft.foundryModel,
        },
      })
      setDraft((d) => ({ ...d, instructions: r.content, mdOverride: null }))
    } catch (e) {
      setGenError((e as Error).message)
    } finally {
      setGenerating(false)
    }
  }

  const applyTemplate = (id: string) => {
    const t = TEMPLATES[id]
    setDraft((d) => ({
      ...d,
      template: id,
      builtinEndpoints: t.builtin,
      sandbox: t.sandbox,
      trigger: t.trigger,
      instructions: t.instructions,
      mdOverride: null,
    }))
  }

  const slug = slugify(draft.name)
  const fileName = `${slug}.agent.md`
  const composed = composeAgentMd(draft)
  const previewMd = draft.mdOverride ?? composed

  const deployJob = useDeployJob()
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
            appName: draft.newApp.appName,
            resourceGroup: draft.newApp.resourceGroup,
            region: draft.newApp.region,
            foundryEndpoint: draft.foundryEndpoint,
            foundryModel: draft.foundryModel,
            // In pick mode we know the Foundry account, so the backend can
            // auto-grant the new app's identity access to it.
            ...(draft.foundryMode === 'pick' && draft.foundryAccount
              ? {
                  foundryAccount: {
                    subscription: foundrySub,
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
  const foundryReady = !!draft.foundryModel && (draft.foundryMode === 'pick' || !!draft.foundryEndpoint)
  const foundryValid = !!draft.foundryModel && (draft.target === 'existing' || !!draft.foundryEndpoint)
  const canDeploy = nameValid && targetValid && foundryValid && !!selected && deployJob.phase !== 'running'

  const cancel = () => {
    sessionStorage.removeItem(DRAFT_KEY)
    navigate(`/agents/${selected}`)
  }

  return (
    <>
      <div className="breadcrumb">
        Home / <Link to={`/agents/${selected}`}>Agents</Link> / Create
      </div>
      <div className="page-title">
        <h1>Create agent</h1>
        <span className="badge gray">draft saved in this session</span>
      </div>
      <p className="page-sub">
        Pick a Foundry model, describe the agent, and deploy it to a Function App. This draft is kept only
        for this browser session.
      </p>

      <div className="steps">
        <span className={'step' + (step === 1 ? ' active' : ' done')}>1 · Foundry model</span>
        <span className="step-sep">→</span>
        <span className={'step' + (step === 2 ? ' active' : '')}>2 · Configure agent</span>
      </div>

      {step === 1 && (
        <>
          <div className="card">
            <h3>Every agent needs a Foundry model</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              Pick a deployed model from any subscription — or enter its details — to continue. The model runs
              your agent and powers ✨ Generate.
            </p>
            <div style={{ display: 'flex', gap: 16, marginBottom: 4 }}>
              <label className="check" style={{ marginBottom: 0 }}>
                <input
                  type="radio"
                  name="fmode"
                  checked={draft.foundryMode === 'pick'}
                  onChange={() => setFoundryMode('pick')}
                />{' '}
                Select a deployed model
              </label>
              <label className="check" style={{ marginBottom: 0 }}>
                <input
                  type="radio"
                  name="fmode"
                  checked={draft.foundryMode === 'manual'}
                  onChange={() => setFoundryMode('manual')}
                />{' '}
                Enter manually
              </label>
            </div>

            {draft.foundryMode === 'pick' ? (
              <>
                <div className="field" style={{ marginBottom: 8 }}>
                  <label>Subscription</label>
                  <select value={foundrySub} onChange={(e) => selectFoundrySub(e.target.value)}>
                    {subscriptions.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name}
                      </option>
                    ))}
                  </select>
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>
                  <select
                    value={draft.foundryAccount}
                    onChange={(e) => selectAccount(e.target.value)}
                    style={{ flex: 1 }}
                  >
                    <option value="">
                      {foundryLoading
                        ? 'Loading Foundry resources…'
                        : foundryAccounts.length
                          ? 'Select a Foundry resource…'
                          : 'No Foundry resources found'}
                    </option>
                    {foundryAccounts.map((a) => (
                      <option key={a.name} value={a.name}>
                        {a.name} · {a.location}
                      </option>
                    ))}
                  </select>
                  <button className="btn sm" onClick={() => void refetchFoundry()} title="Refresh Foundry list">
                    ↻
                  </button>
                </div>

                {selectedAccount && (
                  <div className="grid cols-2" style={{ gap: 12 }}>
                    {selectedAccount.projects.length > 0 && (
                      <div className="field" style={{ marginBottom: 0 }}>
                        <label>Project</label>
                        <select value={draft.foundryEndpoint} onChange={(e) => set('foundryEndpoint', e.target.value)}>
                          <option value="">Select a project…</option>
                          {selectedAccount.projects.map((p) => (
                            <option key={p.name} value={p.endpoint}>
                              {p.name}
                            </option>
                          ))}
                        </select>
                      </div>
                    )}
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Model deployment</label>
                      <select value={draft.foundryModel} onChange={(e) => set('foundryModel', e.target.value)}>
                        <option value="">
                          {selectedAccount.models.length ? 'Select a model…' : 'No chat models deployed'}
                        </option>
                        {selectedAccount.models.map((m) => (
                          <option key={m.deployment} value={m.deployment}>
                            {m.deployment} ({m.model})
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                )}
                <div className="hint" style={{ marginTop: 8 }}>
                  No model yet?{' '}
                  <a href="https://ai.azure.com" target="_blank" rel="noreferrer">
                    Create one in Azure AI Foundry ↗
                  </a>
                  , then ↻ Refresh. A selected model powers ✨ Generate.
                </div>
              </>
            ) : (
              <div className="grid cols-2" style={{ gap: 12, marginTop: 8 }}>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Model / deployment name</label>
                  <input
                    type="text"
                    value={draft.foundryModel}
                    placeholder="gpt-4o"
                    onChange={(e) => set('foundryModel', e.target.value)}
                  />
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Foundry project endpoint</label>
                  <input
                    type="url"
                    value={draft.foundryEndpoint}
                    placeholder="https://<account>.services.ai.azure.com/api/projects/<project>"
                    onChange={(e) => set('foundryEndpoint', e.target.value)}
                  />
                </div>
                <div className="hint" style={{ gridColumn: '1 / -1' }}>
                  Manual entry — you’ll write the instructions yourself (✨ Generate needs a selected model).
                </div>
              </div>
            )}
          </div>
          <div className="toolbar" style={{ marginTop: 16 }}>
            <button className="btn primary" disabled={!foundryReady} onClick={() => setStep(2)}>
              Continue →
            </button>
            <button className="btn" onClick={cancel}>
              Cancel
            </button>
            {!foundryReady && (
              <span className="muted" style={{ fontSize: 12 }}>
                Select or enter a Foundry model to continue.
              </span>
            )}
          </div>
        </>
      )}

      {step === 2 && (
        <>
          <div className="toolbar" style={{ marginBottom: 8 }}>
            <button className="btn sm" onClick={() => setStep(1)}>
              ← Model
            </button>
            <span className="badge blue">
              <span className="dot" /> {draft.foundryModel || 'no model'}
            </span>
            {draft.foundryMode === 'pick' && draft.foundryAccount && (
              <span className="muted" style={{ fontSize: 12 }}>{draft.foundryAccount}</span>
            )}
          </div>
          <div className="grid cols-2" style={{ alignItems: 'start' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div className="card">
            <h3>Agent basics</h3>
            <div className="field">
              <label>Agent name</label>
              <input
                type="text"
                value={draft.name}
                placeholder="support-triage"
                onChange={(e) => set('name', e.target.value)}
              />
              <div className="hint">
                Becomes the file <span className="mono">{fileName}</span> and route slug.
              </div>
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Template</label>
              <select value={draft.template} onChange={(e) => applyTemplate(e.target.value)}>
                {Object.entries(TEMPLATES).map(([id, t]) => (
                  <option key={id} value={id}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="card">
            <h3>Endpoints &amp; trigger</h3>
            <label className="check">
              <input
                type="checkbox"
                checked={draft.builtinEndpoints}
                onChange={(e) => set('builtinEndpoints', e.target.checked)}
              />{' '}
              Built-in endpoints (chat UI, chat API, SSE, MCP tool)
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={draft.sandbox}
                onChange={(e) => set('sandbox', e.target.checked)}
              />{' '}
              Sandbox — ACA Dynamic Sessions <span className="mono">execute_python</span> tool
            </label>
            <div className="field" style={{ marginTop: 12, marginBottom: 0 }}>
              <label>Trigger</label>
              <select
                value={draft.trigger}
                onChange={(e) => set('trigger', e.target.value as Trigger)}
              >
                <option value="http">HTTP</option>
                <option value="timer">Timer</option>
                <option value="connector">Connector</option>
              </select>
            </div>
          </div>

          <div className="card">
            <h3>Function App</h3>
            <div className="field">
              <label>Subscription</label>
              <select value={targetSub} onChange={(e) => selectTargetSub(e.target.value)}>
                {subscriptions.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </div>
            <label className="check">
              <input
                type="radio"
                name="target"
                checked={draft.target === 'existing'}
                onChange={() => set('target', 'existing')}
              />{' '}
              Add to an existing Function App
            </label>
            {draft.target === 'existing' && (
              <div className="field" style={{ margin: '8px 0 4px 24px' }}>
                <select value={draft.existingApp} onChange={(e) => set('existingApp', e.target.value)}>
                  <option value="">
                    {appsLoading
                      ? 'Loading apps…'
                      : apps.length
                        ? 'Select a Function App…'
                        : 'No agent apps in this subscription'}
                  </option>
                  {apps.map((a) => (
                    <option key={a.name} value={a.name}>
                      {a.name} ({a.resourceGroup})
                    </option>
                  ))}
                </select>
                <div className="hint">One Function App can host many agents.</div>
              </div>
            )}

            <label className="check">
              <input
                type="radio"
                name="target"
                checked={draft.target === 'new'}
                onChange={() => set('target', 'new')}
              />{' '}
              Create a new Function App (Flex Consumption)
            </label>
            {draft.target === 'new' && (
              <div style={{ margin: '8px 0 0 24px' }}>
                <div className="grid cols-2" style={{ gap: 12 }}>
                  <div className="field">
                    <label>Function App name</label>
                    <input
                      type="text"
                      value={draft.newApp.appName}
                      placeholder="func-my-agents"
                      onChange={(e) => setNewApp('appName', e.target.value)}
                    />
                    <div className="hint">Globally unique across *.azurewebsites.net.</div>
                  </div>
                  <div className="field">
                    <label>Region</label>
                    <select
                      value={draft.newApp.region}
                      onChange={(e) => setNewApp('region', e.target.value)}
                    >
                      {FLEX_REGIONS.map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Resource group</label>
                  <div style={{ display: 'flex', gap: 16, margin: '2px 0 6px' }}>
                    <label className="check" style={{ marginBottom: 0 }}>
                      <input
                        type="radio"
                        name="rgmode"
                        checked={draft.newApp.rgMode === 'existing'}
                        onChange={() => setNewApp('rgMode', 'existing')}
                      />{' '}
                      Use existing
                    </label>
                    <label className="check" style={{ marginBottom: 0 }}>
                      <input
                        type="radio"
                        name="rgmode"
                        checked={draft.newApp.rgMode === 'new'}
                        onChange={() => setNewApp('rgMode', 'new')}
                      />{' '}
                      Create new
                    </label>
                  </div>
                  {draft.newApp.rgMode === 'existing' ? (
                    <select
                      value={draft.newApp.resourceGroup}
                      onChange={(e) => setNewApp('resourceGroup', e.target.value)}
                    >
                      <option value="">
                        {rgLoading
                          ? 'Loading resource groups…'
                          : resourceGroups.length
                            ? 'Select a resource group…'
                            : 'No resource groups found'}
                      </option>
                      {resourceGroups.map((g) => (
                        <option key={g.name} value={g.name}>
                          {g.name} · {g.location}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      type="text"
                      value={draft.newApp.resourceGroup}
                      placeholder="rg-my-agents"
                      onChange={(e) => setNewApp('resourceGroup', e.target.value)}
                    />
                  )}
                </div>
                <div className="hint" style={{ marginTop: 8 }}>
                  Reuses the Foundry model from step 1 (
                  <span className="mono">{draft.foundryModel || '—'}</span>) — no Foundry is provisioned.
                </div>
              </div>
            )}
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div className="card">
            <h3>✨ Describe your agent</h3>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              Say what the agent should do, then generate its instructions with{' '}
              <span className="mono">{draft.foundryModel}</span>. Everything below stays editable.
            </p>
            <textarea
              className="editor"
              style={{ minHeight: 96 }}
              spellCheck={false}
              placeholder="e.g. Triage inbound support tickets: classify urgency, summarize the issue, and draft a concise reply."
              value={draft.description}
              onChange={(e) => set('description', e.target.value)}
              aria-label="Describe your agent"
            />
            <div className="toolbar" style={{ marginTop: 10 }}>
              <button className="btn primary" onClick={generate} disabled={!canGenerate}>
                {generating ? '✨ Generating…' : '✨ Generate instructions'}
              </button>
              {draft.foundryMode === 'manual' && (
                <span className="muted" style={{ fontSize: 12 }}>
                  Generation needs a picked model (← Model), not manual entry.
                </span>
              )}
              {draft.foundryMode === 'pick' && !draft.description.trim() && (
                <span className="muted" style={{ fontSize: 12 }}>Describe the agent to generate.</span>
              )}
            </div>
            {genError && (
              <p className="muted" style={{ color: 'var(--red)', fontSize: 12, margin: '8px 0 0' }}>
                Generation failed: {genError}
              </p>
            )}
          </div>

          <div className="card" style={{ position: 'sticky', top: 12 }}>
            <div className="card-head">
              <h3 className="mono" style={{ margin: 0 }}>
                {fileName}
              </h3>
              {draft.mdOverride != null ? (
                <button className="btn sm" onClick={() => set('mdOverride', null)} title="Recompose from the fields">
                  ↺ Reset
                </button>
              ) : (
                <span className="badge gray">live preview</span>
              )}
            </div>
            <textarea
              className="editor"
              spellCheck={false}
              value={previewMd}
              onChange={(e) => set('mdOverride', e.target.value)}
              aria-label="Agent definition preview"
            />
          </div>
        </div>
      </div>

      <div className="toolbar" style={{ marginTop: 18 }}>
        <button className="btn primary" disabled={!canDeploy} onClick={runDeploy}>
          {deployJob.phase === 'running'
            ? 'Deploying…'
            : draft.target === 'new'
              ? 'Create Function App & deploy'
              : 'Deploy to Function App'}
        </button>
        <button className="btn" onClick={cancel}>
          Cancel
        </button>
        {!foundryValid && (
          <span className="muted" style={{ fontSize: 12 }}>Pick a Foundry project (← Model) to set the endpoint.</span>
        )}
        {foundryValid && !nameValid && <span className="muted" style={{ fontSize: 12 }}>Enter an agent name.</span>}
        {foundryValid && nameValid && !targetValid && (
          <span className="muted" style={{ fontSize: 12 }}>Choose or configure a Function App.</span>
        )}
      </div>

      <DeploymentStatus
        phase={deployJob.phase}
        result={deployJob.result}
        portalUrl={deployJob.portalUrl}
        message={deployJob.message}
        grant={
          draft.foundryMode === 'pick' && draft.foundryAccount
            ? {
                subscription: foundrySub,
                resourceGroup: draft.foundryResourceGroup,
                account: draft.foundryAccount,
                tenantId: identity?.user?.tenantId,
              }
            : undefined
        }
      />
        </>
      )}
    </>
  )
}
