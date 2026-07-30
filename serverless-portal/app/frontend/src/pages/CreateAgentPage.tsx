import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '../api'
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
  resourceGroup: string
  region: string
  appName: string
  foundryEndpoint: string
  foundryModel: string
}

interface Draft {
  name: string
  description: string
  template: string
  provider: string
  model: string
  builtinEndpoints: boolean
  sandbox: boolean
  trigger: Trigger
  instructions: string
  mdOverride: string | null
  target: 'existing' | 'new'
  existingApp: string
  newApp: NewApp
}

const DEFAULT_DRAFT: Draft = {
  name: '',
  description: '',
  template: 'chat',
  provider: 'foundry',
  model: 'gpt-5.4',
  builtinEndpoints: true,
  sandbox: false,
  trigger: 'http',
  instructions: TEMPLATES.chat.instructions,
  mdOverride: null,
  target: 'existing',
  existingApp: '',
  newApp: { resourceGroup: '', region: 'westus3', appName: '', foundryEndpoint: '', foundryModel: 'gpt-5.4' },
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
  if (d.model) lines.push(`model: ${d.model}`)
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
  const { selected, subscriptions } = useIdentity()
  const [draft, setDraft] = useState<Draft>(loadDraft)

  // Persist to sessionStorage on every change (auto-save for the session).
  useEffect(() => {
    try {
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft))
    } catch {
      /* storage full/unavailable — non-fatal */
    }
  }, [draft])

  const snapshot = useMemo(() => readAgentsSnapshot(selected), [selected])
  const { data } = useQuery({
    queryKey: queryKeys.liveAgents(selected),
    queryFn: () => api.liveAgents(selected),
    enabled: !!selected,
    staleTime: Infinity,
    refetchOnMount: false,
    initialData: snapshot?.data,
    initialDataUpdatedAt: snapshot?.updatedAt,
  })
  const apps = data?.apps ?? []

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }))
  const setNewApp = <K extends keyof NewApp>(key: K, value: NewApp[K]) =>
    setDraft((d) => ({ ...d, newApp: { ...d.newApp, [key]: value } }))

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

  const subName = subscriptions.find((s) => s.id === selected)?.name ?? selected

  const deploy = useMutation({
    mutationFn: () => {
      const target =
        draft.target === 'existing'
          ? {
              kind: 'existing' as const,
              app: draft.existingApp,
              resourceGroup: apps.find((a) => a.name === draft.existingApp)?.resourceGroup ?? '',
            }
          : { kind: 'new' as const, ...draft.newApp }
      return api.deployAgent({
        subscription: selected,
        agent: { fileName, content: previewMd },
        target,
      })
    },
  })

  const nameValid = draft.name.trim().length > 0
  const targetValid =
    draft.target === 'existing'
      ? !!draft.existingApp
      : !!draft.newApp.appName && !!draft.newApp.resourceGroup && !!draft.newApp.region
  const canDeploy = nameValid && targetValid && !!selected && !deploy.isPending

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
        Add an agent to one of your Function Apps — or spin up a new one — in <strong>{subName}</strong>.
        This draft is kept only for this browser session.
      </p>

      <div className="grid cols-2" style={{ alignItems: 'start' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div className="card">
            <h3>1 · Basics</h3>
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
            <div className="field">
              <label>Description</label>
              <input
                type="text"
                value={draft.description}
                placeholder="Triages inbound tickets and drafts a reply."
                onChange={(e) => set('description', e.target.value)}
              />
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
            <h3>2 · Model</h3>
            <div className="grid cols-2" style={{ gap: 12 }}>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Provider</label>
                <select value={draft.provider} onChange={(e) => set('provider', e.target.value)}>
                  <option value="foundry">Foundry</option>
                  <option value="azureopenai">Azure OpenAI</option>
                  <option value="openai">OpenAI</option>
                </select>
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Model / deployment</label>
                <input type="text" value={draft.model} onChange={(e) => set('model', e.target.value)} />
              </div>
            </div>
          </div>

          <div className="card">
            <h3>3 · Endpoints &amp; trigger</h3>
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
            <h3>4 · Function App</h3>
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
                  <option value="">Select a Function App…</option>
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
                  <div className="field">
                    <label>Resource group</label>
                    <input
                      type="text"
                      value={draft.newApp.resourceGroup}
                      placeholder="rg-my-agents (new or existing)"
                      onChange={(e) => setNewApp('resourceGroup', e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <label>Foundry model</label>
                    <input
                      type="text"
                      value={draft.newApp.foundryModel}
                      onChange={(e) => setNewApp('foundryModel', e.target.value)}
                    />
                  </div>
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Existing Foundry project endpoint</label>
                  <input
                    type="url"
                    value={draft.newApp.foundryEndpoint}
                    placeholder="https://<account>.services.ai.azure.com/api/projects/<project>"
                    onChange={(e) => setNewApp('foundryEndpoint', e.target.value)}
                  />
                  <div className="hint">Reused (not provisioned) — fastest and cheapest.</div>
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="card" style={{ position: 'sticky', top: 12 }}>
          <div className="card-head">
            <h3 className="mono" style={{ margin: 0 }}>
              {fileName}
            </h3>
            {draft.mdOverride != null ? (
              <button className="btn sm" onClick={() => set('mdOverride', null)} title="Regenerate from the fields">
                ↺ Reset to fields
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

      <div className="toolbar" style={{ marginTop: 18 }}>
        <button className="btn primary" disabled={!canDeploy} onClick={() => deploy.mutate()}>
          {deploy.isPending
            ? 'Deploying…'
            : draft.target === 'new'
              ? 'Create Function App & deploy'
              : 'Deploy to Function App'}
        </button>
        <button className="btn" onClick={cancel}>
          Cancel
        </button>
        {!nameValid && <span className="muted" style={{ fontSize: 12 }}>Enter an agent name.</span>}
        {nameValid && !targetValid && (
          <span className="muted" style={{ fontSize: 12 }}>Choose or configure a Function App.</span>
        )}
      </div>

      {deploy.isError && (
        <p className="muted" style={{ color: 'var(--red)', marginTop: 10 }}>
          Deploy failed: {(deploy.error as Error).message}
        </p>
      )}
      {deploy.data && (
        <div className="note" style={{ marginTop: 12 }}>
          <strong style={deploy.data.status === 'error' ? { color: 'var(--red)' } : undefined}>
            {deploy.data.status === 'deployed'
              ? 'Deployed.'
              : deploy.data.status === 'error'
                ? 'Deploy failed.'
                : 'Staged.'}
          </strong>{' '}
          {deploy.data.message}
          {deploy.data.status === 'deployed' && deploy.data.url && (
            <div style={{ marginTop: 6 }}>
              URL:{' '}
              <a href={deploy.data.url} target="_blank" rel="noreferrer">
                {deploy.data.url}
              </a>
            </div>
          )}
          {deploy.data.files?.length > 0 && (
            <div style={{ marginTop: 6 }}>
              Source:{' '}
              {deploy.data.files.map((f) => (
                <span key={f} className="badge gray mono" style={{ marginRight: 6 }}>
                  {f}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </>
  )
}
