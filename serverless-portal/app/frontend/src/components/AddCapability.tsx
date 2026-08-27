// "Add capability" flow for the agent edit page — adds a trigger, a connector
// trigger, or an MCP tool server to an agent, writing the change into a portal
// draft of the agent's `.agent.md` or the app's `mcp.json`.

import { useEffect, useState } from 'react'
import { useQueryClient, useQuery } from '@tanstack/react-query'
import { ApiError, api, type CustomToolPreview, type RuntimeIdentity } from '../api'
import { Modal } from './Modal'
import { SearchableSelect, Icon, type IconName } from './ui'
import { Button, Checkbox, Input, Textarea } from '@coreai/fluentui-react'
import {
  TRIGGER_SPECS,
  SCHEDULE_PRESETS,
  buildTriggerYaml,
  applyTriggerToMarkdown,
  addMcpServer,
  MCP_PRESETS,
  skillSlug,
  buildSkillMd,
  type McpServer,
} from '../capabilities'

type View =
  | 'gallery'
  | 'schedule'
  | 'http'
  | 'outlook'
  | 'trigger-advanced'
  | 'mcp-advanced'
  | 'azure-rest'
  | 'tool-ai'
  | 'skill'

export function AddCapability({
  subscription,
  resourceGroup,
  app,
  agentName: agentNameProp,
  agents,
  variant = 'card',
  scope = 'all',
  buttonLabel,
}: {
  subscription: string
  resourceGroup: string
  app: string
  agentName: string
  // When provided (app-level entry point), the user can retarget the capability
  // to any of these agents inside the modal. Omitted for the per-agent panel.
  agents?: string[]
  // 'card' renders the standalone explanatory card (per-agent panel); 'button'
  // renders just a compact trigger for a toolbar (app-level).
  variant?: 'card' | 'button'
  scope?: 'all' | 'triggers' | 'capabilities'
  buttonLabel?: string
}) {
  const qc = useQueryClient()
  // The agent a trigger will be written to. Seeded from the prop; retargetable
  // inside the modal when an `agents` list is supplied.
  const [agentName, setAgentName] = useState(agentNameProp)
  useEffect(() => setAgentName(agentNameProp), [agentNameProp])
  const [open, setOpen] = useState(false)
  const [view, setView] = useState<View>('gallery')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')

  // Trigger form state.
  const [trigType, setTrigType] = useState<string>('http')
  const [trigValues, setTrigValues] = useState<Record<string, string>>({})
  const [httpRoute, setHttpRoute] = useState('')

  // MCP form state.
  const [mcpName, setMcpName] = useState('')
  const [mcpUrl, setMcpUrl] = useState('')
  const [mcpTools, setMcpTools] = useState('')
  const [mcpScope, setMcpScope] = useState('')
  const [mcpClientId, setMcpClientId] = useState('')
  const [mcpPreview, setMcpPreview] = useState('')

  // Skill form state.
  const [skillName, setSkillName] = useState('')
  const [skillDesc, setSkillDesc] = useState('')
  const [skillBody, setSkillBody] = useState('')

  const reset = () => {
    setMsg('')
    setError('')
  }

  const applyTrigger = async (specKey: string, valuesOverride?: Record<string, string>) => {
    reset()
    setBusy(true)
    try {
      const def = await api.getAgentDefinition({ subscription, app, resourceGroup, name: agentName })
      const yaml = buildTriggerYaml(specKey, valuesOverride ?? trigValues)
      const next = applyTriggerToMarkdown(def.content || '', yaml)
      await api.saveAgentDefinition({ subscription, app, name: agentName, content: next })
      qc.invalidateQueries({ queryKey: ['agentDefinition', subscription, app, agentName] })
      const label = specKey === 'connector' ? 'connector' : TRIGGER_SPECS[specKey].type
      setMsg(`Saved a draft of ${agentName}.agent.md with the ${label} trigger.`)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const applyMcp = async (server: McpServer, name: string) => {
    reset()
    setBusy(true)
    try {
      let current = ''
      try {
        current = (await api.getSource({ subscription, app, resourceGroup, path: 'mcp.json' })).content || ''
      } catch {
        current = ''
      }
      const next = addMcpServer(current, name, server)
      await api.saveSource({ subscription, app, path: 'mcp.json', content: next })
      qc.invalidateQueries({ queryKey: ['source', subscription, app, 'mcp.json'] })
      qc.invalidateQueries({ queryKey: ['sourceList', subscription, resourceGroup, app] })
      setMcpPreview(next)
      setMsg(`Saved a draft of mcp.json with the "${name}" server — available to this app's agents.`)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const applyMcpForm = () => {
    if (!mcpName.trim() || !mcpUrl.trim()) {
      setError('Server name and URL are required.')
      return
    }
    const server: McpServer = { type: 'http', url: mcpUrl.trim() }
    const tools = mcpTools
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean)
    if (tools.length) server.tools = tools
    if (mcpScope.trim()) {
      server.auth = { scope: mcpScope.trim(), ...(mcpClientId.trim() ? { client_id: mcpClientId.trim() } : {}) }
    }
    void applyMcp(server, mcpName.trim().replace(/[^A-Za-z0-9._-]/g, '-'))
  }

  const applySkill = async () => {
    if (!skillName.trim() || !skillDesc.trim()) {
      setError('Skill name and description are required.')
      return
    }
    reset()
    setBusy(true)
    try {
      const path = `skills/${skillSlug(skillName)}/SKILL.md`
      const content = buildSkillMd(skillName, skillDesc, skillBody)
      await api.saveSource({ subscription, app, path, content })
      qc.invalidateQueries({ queryKey: ['source', subscription, app, path] })
      qc.invalidateQueries({ queryKey: ['sourceList', subscription, resourceGroup, app] })
      setMsg(`Saved a draft of ${path} — the runtime auto-discovers it as a skill.`)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const spec = TRIGGER_SPECS[trigType]
  const triggerIncomplete = spec.fields.some(
    (f) => f.required && !(trigValues[f.name] ?? f.default ?? '').trim(),
  )
  const outlookPreset = MCP_PRESETS.find((p) => p.name === 'office365-outlook')
  const openModal = () => {
    setOpen(true)
    setView('gallery')
    reset()
  }
  const backToGallery = () => {
    setView('gallery')
    reset()
  }

  return (
    <>
      {variant === 'button' ? (
        <Button size="small" onClick={openModal} title="Add a trigger, tool, or skill to this app">
          ＋ {buttonLabel ?? (scope === 'triggers' ? 'Change trigger' : 'Add capability')}
        </Button>
      ) : (
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-head">
            <h3 style={{ margin: 0, display: 'inline-flex', alignItems: 'center', gap: 8 }}>
              <Icon name="plus" size={18} /> Add capability
            </h3>
            <Button size="small" onClick={openModal}>
              Add a trigger or tool
            </Button>
          </div>
          <p className="muted" style={{ fontSize: 12, margin: 0 }}>
            Pick a ready-made recipe to make <span className="mono">{agentName}</span> run (a trigger) or give it new
            abilities (a tool or skill). Every change saves as a draft — publish with <strong>Deploy edits</strong> or
            open a PR.
          </p>
        </div>
      )}

      {open && (
        <Modal title="Add a capability" onClose={() => setOpen(false)} width={760}>
          {error && (
            <div className="gh-err" style={{ marginBottom: 12 }}>
              {error}
            </div>
          )}
          {msg && (
            <div className="note ok" style={{ marginBottom: 12 }}>
              ✓ {msg}
            </div>
          )}

          {view === 'gallery' && (
            <>
              {agents && agents.length > 1 && (
                <div className="field" style={{ marginBottom: 14 }}>
                  <label>Add to agent</label>
                  <SearchableSelect
                    value={agentName}
                    onChange={(v) => setAgentName(v)}
                    options={agents.map((a) => ({ value: a, label: a }))}
                    placeholder="Select an agent…"
                    ariaLabel="Target agent for this capability"
                  />
                  <div className="hint">
                    Triggers attach to this agent’s <span className="mono">.agent.md</span>. Tools &amp; skills are
                    shared by all agents.
                  </div>
                </div>
              )}
              {scope !== 'capabilities' && (
                <>
                  <div className="recipe-section-label">Make it run — triggers</div>
                  <div className="recipe-grid">
                    <RecipeCard icon="clock" title="On a schedule" desc="Run every hour, daily, or weekly." onClick={() => { setView('schedule'); reset() }} />
                    <RecipeCard icon="globe" title="HTTP endpoint" desc="Call the agent with a web request." onClick={() => { setHttpRoute(agentName); setView('http'); reset() }} />
                    <RecipeCard icon="mail" title="On new Outlook email" desc="React to incoming mail (connector)." onClick={() => { setView('outlook'); reset() }} />
                    <RecipeCard icon="zap" title="More trigger types…" desc="Queue, Service Bus, Blob, Event Grid." onClick={() => { setView('trigger-advanced'); reset() }} />
                  </div>
                </>
              )}

              {scope !== 'triggers' && (
                <>
                  <div className="recipe-section-label">Give it tools &amp; knowledge</div>
                  <div className="recipe-grid">
                {scope === 'capabilities' && (
                  <RecipeCard icon="mail" title="Connector trigger" desc="Start this skill from a connected service event." onClick={() => { setView('outlook'); reset() }} />
                )}
                {MCP_PRESETS.map((p) => (
                  <RecipeCard
                    key={p.name}
                    icon={p.name === 'microsoft-learn' ? 'book' : 'mail'}
                    title={p.label}
                    desc={p.description ?? 'Ready-made MCP tool server.'}
                    disabled={busy}
                    onClick={() => void applyMcp(p.server, p.name)}
                  />
                ))}
                <RecipeCard
                  icon="server"
                  title="Custom MCP server"
                  desc="Connect any remote MCP endpoint."
                  onClick={() => {
                    setView('mcp-advanced')
                    reset()
                  }}
                />
                <RecipeCard
                  icon="terminal"
                  title="Azure REST tool"
                  desc="Call Azure management APIs with the app identity."
                  onClick={() => {
                    setView('azure-rest')
                    reset()
                  }}
                />
                <RecipeCard
                  icon="terminal"
                  title="Generate Python tool"
                  desc="Use this app's configured model to write a tool."
                  onClick={() => {
                    setView('tool-ai')
                    reset()
                  }}
                />
                <RecipeCard
                  icon="bulb"
                  title="Skill"
                  desc="Reusable knowledge in Markdown."
                  onClick={() => {
                    setView('skill')
                    reset()
                  }}
                />
                  </div>
                </>
              )}
            </>
          )}

          {view === 'schedule' && (
            <>
              <button className="view-back" onClick={backToGallery}>
                <Icon name="arrowLeft" size={14} /> All capabilities
              </button>
              <h4 style={{ margin: '0 0 4px', display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                <Icon name="clock" size={16} /> Run on a schedule
              </h4>
              <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
                Choose how often <span className="mono">{agentName}</span> runs. Writes a timer trigger into its{' '}
                <span className="mono">.agent.md</span> — no cron syntax required.
              </p>
              <div className="schedule-grid">
                {SCHEDULE_PRESETS.map((s) => (
                  <Button
                    key={s.cron}
                    disabled={busy}
                    onClick={() => void applyTrigger('timer', { schedule: s.cron })}
                  >
                    {s.label}
                  </Button>
                ))}
              </div>
              <div className="divider" />
              <div className="field">
                <label>Custom schedule (advanced)</label>
                <Input
                  value={trigValues.schedule ?? ''}
                  placeholder="0 0 */6 * * *"
                  onChange={(_, data) => setTrigValues({ schedule: data.value })}
                />
                <div className="hint">6-field NCRONTAB: second minute hour day month weekday.</div>
              </div>
              <Button
                appearance="primary"
                disabled={busy || !(trigValues.schedule ?? '').trim()}
                onClick={() => void applyTrigger('timer')}
              >
                {busy ? 'Applying…' : 'Apply custom schedule'}
              </Button>
              <AiGenerate
                kind="timer_trigger"
                triggerType="timer_trigger"
                title="Generate a scheduled agent with AI"
                hint="Describe the recurring task; a Foundry model writes a complete .agent.md (timer trigger + instructions). Applying replaces this agent's draft."
                subscription={subscription}
                app={app}
                agentName={agentName}
              />
            </>
          )}

          {view === 'http' && (
            <>
              <button className="view-back" onClick={backToGallery}>
                <Icon name="arrowLeft" size={14} /> All capabilities
              </button>
              <h4 style={{ margin: '0 0 4px', display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                <Icon name="globe" size={16} /> HTTP endpoint
              </h4>
              <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
                Exposes <span className="mono">POST /&lt;route&gt;</span> that runs the agent. Writes an http trigger
                into <span className="mono">{agentName}.agent.md</span>.
              </p>
              <div className="field">
                <label>Route</label>
                <Input value={httpRoute} placeholder={agentName} onChange={(_, data) => setHttpRoute(data.value)} />
                <div className="hint">The path callers POST to (defaults to the agent name). Auth: function key.</div>
              </div>
              <Button
                appearance="primary"
                disabled={busy || !httpRoute.trim()}
                onClick={() =>
                  void applyTrigger('http', {
                    route: httpRoute.trim(),
                    methods: 'POST',
                    auth_level: 'function',
                  })
                }
              >
                {busy ? 'Applying…' : 'Add HTTP endpoint'}
              </Button>
              <AiGenerate
                kind="http_trigger"
                triggerType="http_trigger"
                title="Generate an HTTP agent with AI"
                hint="Describe the task; a Foundry model writes a complete .agent.md (HTTP trigger + instructions). Applying replaces this agent's draft."
                subscription={subscription}
                app={app}
                agentName={agentName}
              />
            </>
          )}

          {view === 'outlook' && (
            <>
              <button className="view-back" onClick={backToGallery}>
                <Icon name="arrowLeft" size={14} /> All capabilities
              </button>
              <h4 style={{ margin: '0 0 4px', display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                <Icon name="mail" size={16} /> On new Outlook email
              </h4>
              <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
                The agent runs when a new Office 365 Outlook email arrives. This writes the connector trigger into{' '}
                <span className="mono">{agentName}.agent.md</span>. Two things are still needed to go live:
              </p>
              <ul className="muted" style={{ fontSize: 12, marginTop: 0, paddingLeft: 18 }}>
                <li>Create the Outlook connection in Azure (Connector Gateway).</li>
                <li>Add the Outlook email tool so the agent can act — one click below.</li>
              </ul>
              <div className="gh-row">
                <Button appearance="primary" disabled={busy} onClick={() => void applyTrigger('connector')}>
                  {busy ? 'Applying…' : 'Add connector trigger'}
                </Button>
                {outlookPreset && (
                  <Button
                    disabled={busy}
                    onClick={() => void applyMcp(outlookPreset.server, outlookPreset.name)}
                  >
                    Add Outlook email tool
                  </Button>
                )}
              </div>
              <AiGenerate
                kind="connector_trigger"
                title="Generate a connector agent with AI"
                hint="Describe the connector event and task; a Foundry model writes a complete .agent.md (connector trigger + instructions). Applying replaces this agent's draft."
                subscription={subscription}
                app={app}
                agentName={agentName}
              />
            </>
          )}

          {view === 'trigger-advanced' && (
            <>
              <button className="view-back" onClick={backToGallery}>
                <Icon name="arrowLeft" size={14} /> All capabilities
              </button>
              <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
                Declarative: writes the <span className="mono">trigger:</span> block into{' '}
                <span className="mono">{agentName}.agent.md</span>. The runtime registers the Azure Functions binding
                at deploy — no <span className="mono">function_app.py</span> code is produced.
              </p>
              <div className="field">
                <label>Trigger type</label>
                <SearchableSelect
                  value={trigType}
                  onChange={(v) => {
                    setTrigType(v)
                    setTrigValues({})
                    reset()
                  }}
                  options={Object.entries(TRIGGER_SPECS).map(([k, s]) => ({ value: k, label: s.label }))}
                  placeholder="Select a trigger type…"
                  ariaLabel="Trigger type"
                />
              </div>
              {spec.note && (
                <p className="muted" style={{ fontSize: 12 }}>
                  {spec.note}
                </p>
              )}
              {spec.fields.map((f) => (
                <div className="field" key={f.name}>
                  <label>
                    {f.label}
                    {f.required ? ' *' : ''}
                  </label>
                  <Input
                    value={trigValues[f.name] ?? f.default ?? ''}
                    placeholder={f.placeholder}
                    onChange={(_, data) => setTrigValues((v) => ({ ...v, [f.name]: data.value }))}
                  />
                  {f.help && <div className="hint">{f.help}</div>}
                </div>
              ))}
              <Button
                appearance="primary"
                disabled={busy || triggerIncomplete}
                onClick={() => void applyTrigger(trigType)}
              >
                {busy ? 'Applying…' : `Apply ${spec.label} trigger`}
              </Button>
              <AiGenerate
                kind="http_trigger"
                triggerType={spec.type}
                title={`Generate a ${spec.label} agent with AI`}
                hint={`Describe the task; a Foundry model writes a complete .agent.md (${spec.label.toLowerCase()} trigger + instructions). Applying replaces this agent's draft.`}
                subscription={subscription}
                app={app}
                agentName={agentName}
              />
            </>
          )}

          {view === 'mcp-advanced' && (
            <>
              <button className="view-back" onClick={backToGallery}>
                <Icon name="arrowLeft" size={14} /> All capabilities
              </button>
              <h4 style={{ margin: '0 0 4px', display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                <Icon name="server" size={16} /> Custom MCP server
              </h4>
              <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
                Connect a remote HTTP MCP endpoint. Written to <span className="mono">mcp.json</span> and shared by all
                of this app's agents.
              </p>
              <div className="field">
                <label>Server name *</label>
                <Input value={mcpName} placeholder="my-mcp-server" onChange={(_, data) => setMcpName(data.value)} />
              </div>
              <div className="field">
                <label>Server URL *</label>
                <Input value={mcpUrl} placeholder="https://… or $ENV_VAR" onChange={(_, data) => setMcpUrl(data.value)} />
                <div className="hint">Remote HTTP MCP endpoint. Supports $ENV_VAR substitution.</div>
              </div>
              <div className="field">
                <label>Tools (optional)</label>
                <Input
                  value={mcpTools}
                  placeholder="tool_a, tool_b (blank = all)"
                  onChange={(_, data) => setMcpTools(data.value)}
                />
              </div>
              <div className="grid cols-2">
                <div className="field">
                  <label>Auth scope (optional)</label>
                  <Input
                    value={mcpScope}
                    placeholder="https://apihub.azure.com/.default"
                    onChange={(_, data) => setMcpScope(data.value)}
                  />
                </div>
                <div className="field">
                  <label>Client ID (optional)</label>
                  <Input
                    value={mcpClientId}
                    placeholder="$O365_MCP_CLIENT_ID"
                    onChange={(_, data) => setMcpClientId(data.value)}
                  />
                </div>
              </div>
              <Button
                appearance="primary"
                disabled={busy || !mcpName.trim() || !mcpUrl.trim()}
                onClick={applyMcpForm}
              >
                {busy ? 'Applying…' : 'Add MCP server'}
              </Button>
              {mcpPreview && (
                <pre className="code" style={{ marginTop: 12, maxHeight: 220 }}>
                  {mcpPreview}
                </pre>
              )}
            </>
          )}

          {view === 'tool-ai' && (
            <>
              <button className="view-back" onClick={backToGallery}>
                <Icon name="arrowLeft" size={14} /> All capabilities
              </button>
              <h4 style={{ margin: '0 0 4px', display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                <Icon name="terminal" size={16} /> Custom tool (Python)
              </h4>
              <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
                Generates a Python <span className="mono">@tool</span> saved to{' '}
                <span className="mono">tools/&lt;name&gt;.py</span> that this app's agents can call.
              </p>
              <AiGenerate
                kind="custom_tool"
                title="Generate a custom tool (Python) with AI"
                hint="Describe a capability; a Foundry model writes a Python @tool for the tools/ folder that this app's agents can call. Saved as tools/<name>.py."
                subscription={subscription}
                resourceGroup={resourceGroup}
                app={app}
                agentName={agentName}
              />
            </>
          )}

          {view === 'azure-rest' && (
            <AzureRestTool
              subscription={subscription}
              resourceGroup={resourceGroup}
              app={app}
              onBack={backToGallery}
            />
          )}

          {view === 'skill' && (
            <>
              <button className="view-back" onClick={backToGallery}>
                <Icon name="arrowLeft" size={14} /> All capabilities
              </button>
              <h4 style={{ margin: '0 0 4px', display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                <Icon name="bulb" size={16} /> Skill
              </h4>
              <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
                A <strong>skill</strong> is reusable knowledge written to{' '}
                <span className="mono">skills/&lt;name&gt;/SKILL.md</span>. The runtime auto-discovers it and the agent
                pulls it in on demand.
              </p>
              <div className="field">
                <label>Skill name *</label>
                <Input value={skillName} placeholder="api-assistant" onChange={(_, data) => setSkillName(data.value)} />
                <div className="hint">kebab-case, e.g. azure-resources</div>
              </div>
              <div className="field">
                <label>Description *</label>
                <Input
                  value={skillDesc}
                  placeholder="What it provides and when the agent should use it"
                  onChange={(_, data) => setSkillDesc(data.value)}
                />
              </div>
              <div className="field">
                <label>Content (Markdown, optional)</label>
                <Textarea
                  value={skillBody}
                  placeholder={'# Title\n\nDomain knowledge, how-to steps, references, best practices…'}
                  onChange={(_, data) => setSkillBody(data.value)}
                  textarea={{ spellCheck: false, style: { minHeight: '120px' } }}
                />
                <div className="hint">Leave blank to start from a stub, or use Generate below.</div>
              </div>
              <Button
                appearance="primary"
                disabled={busy || !skillName.trim() || !skillDesc.trim()}
                onClick={() => void applySkill()}
              >
                {busy ? 'Applying…' : 'Add skill'}
              </Button>
              <AiGenerate
                kind="skill"
                title="Generate a skill with AI"
                hint="Describe the knowledge/capability; a Foundry model writes a SKILL.md saved to skills/<name>/SKILL.md."
                subscription={subscription}
                app={app}
                agentName={agentName}
              />
            </>
          )}
        </Modal>
      )}
    </>
  )
}

function RecipeCard({
  icon,
  title,
  desc,
  onClick,
  disabled,
}: {
  icon: IconName
  title: string
  desc: string
  onClick: () => void
  disabled?: boolean
}) {
  return (
    <button type="button" className="recipe-card" onClick={onClick} disabled={disabled}>
      <span className="recipe-ico">
        <Icon name={icon} size={18} />
      </span>
      <span className="recipe-body">
        <span className="recipe-title">{title}</span>
        <span className="recipe-desc">{desc}</span>
      </span>
    </button>
  )
}

function AzureRestTool({
  subscription,
  resourceGroup,
  app,
  onBack,
}: {
  subscription: string
  resourceGroup: string
  app: string
  onBack: () => void
}) {
  const qc = useQueryClient()
  const [toolName, setToolName] = useState('azure_rest')
  const [scopeType, setScopeType] = useState<'subscription' | 'resourceGroup'>('subscription')
  const [scopeResourceGroup, setScopeResourceGroup] = useState(resourceGroup)
  const [preview, setPreview] = useState<CustomToolPreview | null>(null)
  const [python, setPython] = useState('')
  const [roleId, setRoleId] = useState('')
  const [identityClientId, setIdentityClientId] = useState('')
  const [overwrite, setOverwrite] = useState(false)
  const [description, setDescription] = useState(
    'Call Azure Resource Manager with path, method, optional JSON body, and optional JMESPath query arguments.',
  )
  const [busy, setBusy] = useState<'preview' | 'generate' | 'save' | 'access' | ''>('')
  const [error, setError] = useState('')
  const [saveResult, setSaveResult] = useState('')
  const [accessResult, setAccessResult] = useState('')

  const identityQuery = useQuery({
    queryKey: ['customToolIdentity', subscription, resourceGroup, app],
    queryFn: () => api.getCustomToolIdentity({ subscription, resourceGroup, app }),
    retry: false,
  })
  const identityError = identityQuery.error instanceof ApiError ? identityQuery.error : null
  const candidates = (identityError?.data.candidates as RuntimeIdentity[] | undefined) ?? []
  const identity = identityQuery.data?.identity
  const rolesQuery = useQuery({
    queryKey: ['customToolRoles', subscription, scopeType, scopeResourceGroup],
    queryFn: () => api.listCustomToolRoles({
      subscription,
      scopeType,
      resourceGroup: scopeType === 'resourceGroup' ? scopeResourceGroup : undefined,
    }),
    enabled: scopeType === 'subscription' || !!scopeResourceGroup.trim(),
    retry: false,
  })
  const roles = rolesQuery.data?.roles ?? []
  const effectiveRole = roleId || roles.find((role) => role.isDefault)?.id || roles[0]?.id || ''
  const modelQuery = useQuery({
    queryKey: ['customToolConfiguredModel', subscription, resourceGroup, app],
    queryFn: () => api.getConfiguredModel({ subscription, resourceGroup, app }),
    retry: false,
  })

  const createPreview = async () => {
    setBusy('preview')
    setError('')
    setSaveResult('')
    try {
      const result = await api.previewAzureRestTool({ subscription, resourceGroup, app, toolName })
      setPreview(result)
      setPython(result.python)
      setOverwrite(false)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  const generate = async () => {
    if (!description.trim()) return
    setBusy('generate')
    setError('')
    try {
      const base = preview ?? await api.previewAzureRestTool({ subscription, resourceGroup, app, toolName })
      setPreview(base)
      const result = await api.generateCapability({
        subscription,
        resourceGroup,
        app,
        kind: 'custom_tool',
        name: toolName,
        description:
          `${description.trim()} Preserve the class name AzureRestParams, the decorator ` +
          '`@tool(schema=AzureRestParams)`, and the Azure REST arguments: path, method, optional body, and optional query.',
        groundInSkills: true,
      })
      setPython(result.content)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  const save = async () => {
    if (!preview) return
    setBusy('save')
    setError('')
    setSaveResult('')
    try {
      const result = await api.saveAzureRestTool({
        subscription,
        resourceGroup,
        app,
        toolName,
        python,
        overwrite,
      })
      setSaveResult(`Saved ${result.toolPath} and merged ${result.requirementsPath} as drafts.`)
      qc.invalidateQueries({ queryKey: ['sourceList', subscription, resourceGroup, app] })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  const grantAccess = async () => {
    setBusy('access')
    setError('')
    setAccessResult('')
    try {
      const result = await api.grantCustomToolAccess({
        subscription,
        resourceGroup,
        app,
        scopeType,
        scopeResourceGroup: scopeType === 'resourceGroup' ? scopeResourceGroup : undefined,
        identityClientId: identityClientId || undefined,
        roleDefinitionId: effectiveRole,
      })
      setAccessResult(
        result.outcome === 'existing'
          ? `${result.identity.name} already has ${result.role.name} at this scope.`
          : `Granted ${result.role.name} to ${result.identity.name}.`,
      )
      qc.invalidateQueries({ queryKey: ['customToolIdentity', subscription, resourceGroup, app] })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  return (
    <>
      <button className="view-back" onClick={onBack}>
        <Icon name="arrowLeft" size={14} /> All capabilities
      </button>
      <h4 style={{ margin: '0 0 4px', display: 'inline-flex', alignItems: 'center', gap: 8 }}>
        <Icon name="terminal" size={16} /> Azure REST tool
      </h4>
      <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
        Creates a managed-identity Azure Resource Manager tool based on the Daily Azure Report sample.
      </p>
      {error && <div className="gh-err" style={{ marginBottom: 12 }}>{error}</div>}

      <div className="field">
        <label>Tool name *</label>
        <Input value={toolName} onChange={(_, data) => setToolName(data.value)} placeholder="azure_rest" />
        <div className="hint">Runtime arguments: path, method, optional JSON body, optional JMESPath query.</div>
      </div>
      <div className="gh-row">
        <Button appearance="primary" disabled={!!busy || !toolName.trim()} onClick={() => void createPreview()}>
          {busy === 'preview' ? 'Preparing…' : 'Create preview'}
        </Button>
      </div>

      {preview && (
        <>
          <div className="divider" />
          <div className="field">
            <label>Python · {preview.toolPath}</label>
            <Textarea
              value={python}
              onChange={(_, data) => setPython(data.value)}
              textarea={{ spellCheck: false, style: { minHeight: '260px', fontFamily: 'var(--font-mono)' } }}
            />
          </div>
          <div className="field">
            <label>Dependencies · requirements.txt</label>
            <pre className="code" style={{ maxHeight: 150 }}>{preview.requirements}</pre>
            <div className="hint">
              {preview.addedDependencies.length
                ? `Will add: ${preview.addedDependencies.join(', ')}.`
                : 'All required dependencies are already present.'}
            </div>
          </div>
          {preview.requiresOverwrite && (
            <Checkbox
              checked={overwrite}
              onChange={(_, data) => setOverwrite(data.checked === true)}
              label={`Replace the existing ${preview.toolPath}`}
            />
          )}
          <Button
            appearance="primary"
            disabled={!!busy || !python.trim() || (preview.requiresOverwrite && !overwrite)}
            onClick={() => void save()}
          >
            {busy === 'save' ? 'Saving…' : 'Save tool and requirements drafts'}
          </Button>
          {saveResult && <div className="note ok" style={{ marginTop: 12 }}>✓ {saveResult}</div>}

          <div className="divider" />
          <h4 style={{ marginBottom: 4, fontSize: 13 }}>Customize with AI</h4>
          <div className="hint" style={{ marginBottom: 8 }}>
            {modelQuery.data
              ? `Uses this app's configured ${modelQuery.data.provider} model: ${modelQuery.data.model}.`
              : modelQuery.isLoading
                ? 'Checking this app’s configured model…'
                : (modelQuery.error as Error | null)?.message ?? 'Configured model unavailable.'}
          </div>
          <div className="field">
            <Textarea
              value={description}
              onChange={(_, data) => setDescription(data.value)}
              textarea={{ spellCheck: true, style: { minHeight: '76px' } }}
            />
          </div>
          <Button disabled={!!busy || !modelQuery.data || !description.trim()} onClick={() => void generate()}>
            {busy === 'generate' ? 'Generating…' : 'Generate Python'}
          </Button>

          <div className="divider" />
          <h4 style={{ marginBottom: 4, fontSize: 13 }}>Azure access</h4>
          <div className="hint" style={{ marginBottom: 10 }}>
            Source drafts are saved independently. Granting access requires role-assignment permission in Azure.
          </div>
          <div className="grid cols-2">
            <div className="field">
              <label>Scope</label>
              <SearchableSelect
                value={scopeType}
                onChange={(value) => setScopeType(value as 'subscription' | 'resourceGroup')}
                options={[
                  { value: 'subscription', label: 'Subscription' },
                  { value: 'resourceGroup', label: 'Resource group' },
                ]}
                ariaLabel="Role scope"
              />
            </div>
            {scopeType === 'resourceGroup' && (
              <div className="field">
                <label>Resource group</label>
                <Input value={scopeResourceGroup} onChange={(_, data) => setScopeResourceGroup(data.value)} />
              </div>
            )}
          </div>
          <div className="field">
            <label>Role</label>
            <SearchableSelect
              value={effectiveRole}
              onChange={setRoleId}
              options={roles.map((role) => ({ value: role.id, label: role.name }))}
              placeholder={rolesQuery.isLoading ? 'Loading roles…' : 'Select a built-in role'}
              loading={rolesQuery.isLoading}
              ariaLabel="Azure role"
            />
            {rolesQuery.error && <div className="hint" style={{ color: 'var(--red)' }}>{(rolesQuery.error as Error).message}</div>}
          </div>
          <div className="field">
            <label>Runtime identity</label>
            {identity ? (
              <div className="note">{identity.name}</div>
            ) : candidates.length ? (
              <SearchableSelect
                value={identityClientId}
                onChange={setIdentityClientId}
                options={candidates.map((candidate) => ({ value: candidate.clientId, label: candidate.name }))}
                placeholder="Choose an attached identity"
                ariaLabel="Runtime identity"
              />
            ) : (
              <div className="hint" style={{ color: identityQuery.error ? 'var(--red)' : undefined }}>
                {identityQuery.isLoading ? 'Resolving managed identity…' : (identityQuery.error as Error | null)?.message}
              </div>
            )}
          </div>
          <Button
            disabled={!!busy || !effectiveRole || (!identity && !identityClientId)}
            onClick={() => void grantAccess()}
          >
            {busy === 'access' ? 'Granting…' : 'Grant Azure access'}
          </Button>
          {accessResult && <div className="note ok" style={{ marginTop: 12 }}>✓ {accessResult}</div>}
        </>
      )}
    </>
  )
}

type GenKind = 'http_trigger' | 'connector_trigger' | 'timer_trigger' | 'custom_tool' | 'skill'

// A Foundry-backed generator: pick a model, describe the capability, generate
// the code/config, preview it, then apply it to a draft (`.agent.md` for
// triggers, `tools/<name>.py` for a custom tool).
function AiGenerate({
  kind,
  triggerType,
  title,
  hint,
  subscription,
  resourceGroup,
  app,
  agentName,
}: {
  kind: GenKind
  triggerType?: string
  title: string
  hint: string
  subscription: string
  resourceGroup?: string
  app: string
  agentName: string
}) {
  const qc = useQueryClient()
  const { data: foundry, error: loadErrObj, isLoading, isFetching, refetch } = useQuery({
    queryKey: ['foundry', subscription],
    queryFn: () => api.listFoundry(subscription),
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: 'always',
    enabled: !!subscription && kind !== 'custom_tool',
  })
  const loadErr = loadErrObj ? (loadErrObj as Error).message : ''
  const configuredModel = useQuery({
    queryKey: ['customToolConfiguredModel', subscription, resourceGroup, app],
    queryFn: () => api.getConfiguredModel({ subscription, resourceGroup: resourceGroup!, app }),
    enabled: kind === 'custom_tool' && !!resourceGroup,
    retry: false,
  })
  const [modelKey, setModelKey] = useState('')
  const [desc, setDesc] = useState('')
  const [toolName, setToolName] = useState('')
  const [ground, setGround] = useState(true)
  const [busy, setBusy] = useState(false)
  const [content, setContent] = useState('')
  const [error, setError] = useState('')
  const [applied, setApplied] = useState('')

  const options = (foundry?.accounts ?? []).flatMap((a, ai) =>
    a.models.map((m, mi) => ({
      key: `${ai}:${mi}`,
      label: `${a.name} · ${m.deployment}`,
      foundry: {
        resourceGroup: a.resourceGroup,
        account: a.name,
        openaiEndpoint: a.openaiEndpoint,
        model: m.deployment,
      },
    })),
  )
  const effectiveKey = modelKey || options[0]?.key || ''
  const selected = options.find((o) => o.key === effectiveKey)

  const generate = async () => {
    if ((!selected && kind !== 'custom_tool') || !desc.trim()) return
    setBusy(true)
    setError('')
    setApplied('')
    try {
      const r = await api.generateCapability({
        subscription,
        resourceGroup,
        app,
        kind,
        triggerType,
        name: kind === 'custom_tool' || kind === 'skill' ? toolName : agentName,
        description: desc.trim(),
        groundInSkills: ground,
        foundry: selected?.foundry,
      })
      setContent(r.content)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const apply = async () => {
    setBusy(true)
    setError('')
    setApplied('')
    try {
      if (kind === 'custom_tool') {
        const slug =
          (toolName || 'custom_tool')
            .trim()
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '_')
            .replace(/^_+|_+$/g, '') || 'custom_tool'
        const path = `tools/${slug}.py`
        await api.saveSource({ subscription, app, path, content })
        qc.invalidateQueries({ queryKey: ['source', subscription, app, path] })
        qc.invalidateQueries({ queryKey: ['sourceList', subscription] })
        setApplied(`Saved a draft of ${path}.`)
      } else if (kind === 'skill') {
        const path = `skills/${skillSlug(toolName)}/SKILL.md`
        await api.saveSource({ subscription, app, path, content })
        qc.invalidateQueries({ queryKey: ['source', subscription, app, path] })
        qc.invalidateQueries({ queryKey: ['sourceList', subscription] })
        setApplied(`Saved a draft of ${path}.`)
      } else {
        await api.saveAgentDefinition({ subscription, app, name: agentName, content })
        qc.invalidateQueries({ queryKey: ['agentDefinition', subscription, app, agentName] })
        setApplied(`Replaced ${agentName}.agent.md draft with the generated definition.`)
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ marginTop: 16, borderTop: '1px solid var(--border)', paddingTop: 14 }}>
      <h4 style={{ margin: '0 0 4px', fontSize: 13 }}>✨ {title}</h4>
      <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
        {hint}
      </p>
      {kind === 'custom_tool' ? (
        <div className="note" style={{ marginBottom: 10 }}>
          {configuredModel.data
            ? `Using this app's configured ${configuredModel.data.provider} model: ${configuredModel.data.model}.`
            : configuredModel.isLoading
              ? 'Checking this app’s configured model…'
              : (configuredModel.error as Error | null)?.message ?? 'Configured model unavailable.'}
        </div>
      ) : <div className="field">
        <label>Foundry model</label>
        <SearchableSelect
          value={effectiveKey}
          onChange={setModelKey}
          options={options.map((o) => ({ value: o.key, label: o.label }))}
          placeholder={isLoading ? 'Loading Foundry models…' : 'No Foundry models in this subscription'}
          loading={isLoading || isFetching}
          loadingLabel="Refreshing Foundry models…"
          onRefresh={() => void refetch()}
          ariaLabel="Foundry model"
        />
        {loadErr && (
          <div className="hint" style={{ color: 'var(--red)' }}>
            {loadErr}
          </div>
        )}
      </div>}
      {(kind === 'custom_tool' || kind === 'skill') && (
        <div className="field">
          <label>{kind === 'skill' ? 'Skill name *' : 'Tool name *'}</label>
          <Input
            value={toolName}
            placeholder={kind === 'skill' ? 'api-assistant' : 'fetch_weather'}
            onChange={(_, data) => setToolName(data.value)}
          />
        </div>
      )}
      <div className="field">
        <label>Describe what to generate *</label>
        <Textarea
          value={desc}
          placeholder={
            kind === 'custom_tool'
              ? 'e.g. Fetch the current weather for a city from an HTTP API and return a short summary.'
              : kind === 'skill'
                ? 'e.g. How to query Azure Resource Graph: common KQL, endpoints, auth, and pitfalls.'
                : kind === 'connector_trigger'
                  ? 'e.g. When a new Outlook email arrives from my manager, draft a concise reply.'
                  : kind === 'timer_trigger'
                    ? 'e.g. Every weekday at 9am UTC, summarise yesterday\u2019s open GitHub PRs and post the digest to Teams.'
                    : 'e.g. Accept a support ticket, classify urgency, and return a JSON triage result.'
          }
          onChange={(_, data) => setDesc(data.value)}
          textarea={{ spellCheck: false, style: { minHeight: '84px' } }}
        />
      </div>
      <Checkbox
        checked={ground}
        onChange={(_, data) => setGround(data.checked === true)}
        label="Ground in this app's existing skills"
        style={{ marginBottom: 10 }}
      />
      <Button
        appearance="primary"
        disabled={busy || !desc.trim() || (kind === 'custom_tool' ? !configuredModel.data : !selected) || ((kind === 'custom_tool' || kind === 'skill') && !toolName.trim())}
        onClick={() => void generate()}
      >
        {busy && !content ? (
          <>
            <span className="gh-spin" /> Generating…
          </>
        ) : (
          '✨ Generate'
        )}
      </Button>
      {content && (
        <>
          <pre className="code" style={{ marginTop: 12, maxHeight: 320 }}>
            {content}
          </pre>
          <div className="gh-row">
            <Button appearance="primary" disabled={busy} onClick={() => void apply()}>
              {busy
                ? 'Applying…'
                : kind === 'custom_tool'
                  ? 'Save as tool draft'
                  : kind === 'skill'
                    ? 'Save as skill draft'
                    : 'Apply to .agent.md draft'}
            </Button>
            <Button appearance="subtle" size="small" onClick={() => setContent('')}>
              Discard
            </Button>
          </div>
        </>
      )}
      {applied && (
        <div className="note ok" style={{ marginTop: 12 }}>
          ✓ {applied}
        </div>
      )}
      {error && (
        <div className="gh-err" style={{ marginTop: 12 }}>
          {error}
        </div>
      )}
    </div>
  )
}
