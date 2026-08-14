// "Add capability" flow for the agent edit page — adds a trigger, a connector
// trigger, or an MCP tool server to an agent, writing the change into a portal
// draft of the agent's `.agent.md` or the app's `mcp.json`.

import { useState } from 'react'
import { useQueryClient, useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { Modal } from './Modal'
import { SearchableSelect } from './ui'
import {
  TRIGGER_SPECS,
  buildTriggerYaml,
  applyTriggerToMarkdown,
  addMcpServer,
  MCP_PRESETS,
  skillSlug,
  buildSkillMd,
  type McpServer,
} from '../capabilities'

type Mode = 'trigger' | 'connector' | 'mcp' | 'skill'

export function AddCapability({
  subscription,
  resourceGroup,
  app,
  agentName,
}: {
  subscription: string
  resourceGroup: string
  app: string
  agentName: string
}) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<Mode>('trigger')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')

  // Trigger form state.
  const [trigType, setTrigType] = useState<string>('http')
  const [trigValues, setTrigValues] = useState<Record<string, string>>({})

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

  const applyTrigger = async (specKey: string) => {
    reset()
    setBusy(true)
    try {
      const def = await api.getAgentDefinition({ subscription, app, resourceGroup, name: agentName })
      const yaml = buildTriggerYaml(specKey, trigValues)
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

  return (
    <>
      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-head">
          <h3 style={{ margin: 0 }}>➕ Add capability</h3>
          <button className="btn sm" onClick={() => setOpen(true)}>
            Add trigger, connector, MCP tool, or skill
          </button>
        </div>
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          Add a trigger, a connector trigger, an MCP tool, or a reusable skill. Changes are saved as a draft on
          this agent's <span className="mono">.agent.md</span>, the app's <span className="mono">mcp.json</span>,
          or <span className="mono">skills/</span> — publish with <strong>Deploy edits</strong> or open a PR.
        </p>
      </div>

      {open && (
        <Modal title="➕ Add capability" onClose={() => setOpen(false)} width={760}>
          <div className="tabs" style={{ marginBottom: 12 }}>
        <button
          className={'tab' + (mode === 'trigger' ? ' active' : '')}
          onClick={() => {
            setMode('trigger')
            reset()
          }}
        >
          ⏱ Trigger
        </button>
        <button
          className={'tab' + (mode === 'connector' ? ' active' : '')}
          onClick={() => {
            setMode('connector')
            reset()
          }}
        >
          🔌 Connector trigger
        </button>
        <button
          className={'tab' + (mode === 'mcp' ? ' active' : '')}
          onClick={() => {
            setMode('mcp')
            reset()
          }}
        >
          🧰 MCP tool
        </button>
        <button
          className={'tab' + (mode === 'skill' ? ' active' : '')}
          onClick={() => {
            setMode('skill')
            reset()
          }}
        >
          🧠 Skill
        </button>
      </div>

      {mode === 'trigger' && (
        <>
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
              <input
                value={trigValues[f.name] ?? f.default ?? ''}
                placeholder={f.placeholder}
                onChange={(e) => setTrigValues((v) => ({ ...v, [f.name]: e.target.value }))}
              />
              {f.help && <div className="hint">{f.help}</div>}
            </div>
          ))}
          <button className="btn primary" disabled={busy || triggerIncomplete} onClick={() => void applyTrigger(trigType)}>
            {busy ? 'Applying…' : `Apply ${spec.label} trigger`}
          </button>
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

      {mode === 'connector' && (
        <>
          <p className="muted" style={{ fontSize: 13 }}>
            Declarative: writes <span className="mono">trigger: generic_trigger · connectorTrigger</span> into{' '}
            <span className="mono">{agentName}.agent.md</span> — the runtime registers the connector binding at
            deploy (no <span className="mono">function_app.py</span> code). The agent runs when an Azure Connector
            event fires (e.g. a new Outlook email). You still need to: (1) create the connector{' '}
            <strong>connection</strong> in Azure, and (2) add the connector's <strong>MCP tool</strong> (MCP tab)
            so the agent can act.
          </p>
          <button className="btn primary" disabled={busy} onClick={() => void applyTrigger('connector')}>
            {busy ? 'Applying…' : 'Apply connector trigger'}
          </button>
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

      {mode === 'mcp' && (
        <>
          <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
            A <strong>remote MCP server</strong> is configuration written to <span className="mono">mcp.json</span>.
            A <strong>custom tool</strong> is actual Python code written to <span className="mono">tools/&lt;name&gt;.py</span>{' '}
            — use <strong>✨ Generate a custom tool</strong> below.
          </p>
          <div className="field">
            <label>Quick add</label>
            <div className="pill-row">
              {MCP_PRESETS.map((p) => (
                <button key={p.name} className="btn sm" disabled={busy} onClick={() => void applyMcp(p.server, p.name)}>
                  ＋ {p.label}
                </button>
              ))}
            </div>
            <div className="hint">Adds a ready-made server from the runtime samples.</div>
          </div>
          <div className="divider" />
          <div className="field">
            <label>Server name *</label>
            <input value={mcpName} placeholder="my-mcp-server" onChange={(e) => setMcpName(e.target.value)} />
          </div>
          <div className="field">
            <label>Server URL *</label>
            <input value={mcpUrl} placeholder="https://… or $ENV_VAR" onChange={(e) => setMcpUrl(e.target.value)} />
            <div className="hint">Remote HTTP MCP endpoint. Supports $ENV_VAR substitution.</div>
          </div>
          <div className="field">
            <label>Tools (optional)</label>
            <input
              value={mcpTools}
              placeholder="tool_a, tool_b (blank = all)"
              onChange={(e) => setMcpTools(e.target.value)}
            />
          </div>
          <div className="grid cols-2">
            <div className="field">
              <label>Auth scope (optional)</label>
              <input
                value={mcpScope}
                placeholder="https://apihub.azure.com/.default"
                onChange={(e) => setMcpScope(e.target.value)}
              />
            </div>
            <div className="field">
              <label>Client ID (optional)</label>
              <input
                value={mcpClientId}
                placeholder="$O365_MCP_CLIENT_ID"
                onChange={(e) => setMcpClientId(e.target.value)}
              />
            </div>
          </div>
          <button className="btn primary" disabled={busy || !mcpName.trim() || !mcpUrl.trim()} onClick={applyMcpForm}>
            {busy ? 'Applying…' : 'Add MCP server'}
          </button>
          {mcpPreview && (
            <pre className="code" style={{ marginTop: 12, maxHeight: 220 }}>
              {mcpPreview}
            </pre>
          )}
          <AiGenerate
            kind="custom_tool"
            title="Generate a custom tool (Python) with AI"
            hint="Describe a capability; a Foundry model writes a Python @tool for the tools/ folder that this app's agents can call. Saved as tools/<name>.py."
            subscription={subscription}
            app={app}
            agentName={agentName}
          />
        </>
      )}

      {mode === 'skill' && (
        <>
          <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
            A <strong>skill</strong> is reusable knowledge written to{' '}
            <span className="mono">skills/&lt;name&gt;/SKILL.md</span>. The runtime auto-discovers it and the agent
            pulls it in on demand — edit it any time to control and enhance behaviour.
          </p>
          <div className="field">
            <label>Skill name *</label>
            <input value={skillName} placeholder="api-assistant" onChange={(e) => setSkillName(e.target.value)} />
            <div className="hint">kebab-case, e.g. azure-resources</div>
          </div>
          <div className="field">
            <label>Description *</label>
            <input
              value={skillDesc}
              placeholder="What it provides and when the agent should use it"
              onChange={(e) => setSkillDesc(e.target.value)}
            />
          </div>
          <div className="field">
            <label>Content (Markdown, optional)</label>
            <textarea
              className="editor"
              style={{ minHeight: 120 }}
              spellCheck={false}
              value={skillBody}
              placeholder={'# Title\n\nDomain knowledge, how-to steps, references, best practices…'}
              onChange={(e) => setSkillBody(e.target.value)}
            />
            <div className="hint">Leave blank to start from a stub, or use ✨ Generate below.</div>
          </div>
          <button
            className="btn primary"
            disabled={busy || !skillName.trim() || !skillDesc.trim()}
            onClick={() => void applySkill()}
          >
            {busy ? 'Applying…' : 'Add skill'}
          </button>
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

      {msg && (
        <div className="note ok" style={{ marginTop: 12 }}>
          ✓ {msg}
        </div>
      )}
      {error && (
        <div className="gh-err" style={{ marginTop: 12 }}>
          {error}
        </div>
      )}
        </Modal>
      )}
    </>
  )
}

type GenKind = 'http_trigger' | 'connector_trigger' | 'custom_tool' | 'skill'

// A Foundry-backed generator: pick a model, describe the capability, generate
// the code/config, preview it, then apply it to a draft (`.agent.md` for
// triggers, `tools/<name>.py` for a custom tool).
function AiGenerate({
  kind,
  triggerType,
  title,
  hint,
  subscription,
  app,
  agentName,
}: {
  kind: GenKind
  triggerType?: string
  title: string
  hint: string
  subscription: string
  app: string
  agentName: string
}) {
  const qc = useQueryClient()
  const { data: foundry, error: loadErrObj, isLoading } = useQuery({
    queryKey: ['foundry', subscription],
    queryFn: () => api.listFoundry(subscription),
    staleTime: 5 * 60 * 1000,
    enabled: !!subscription,
  })
  const loadErr = loadErrObj ? (loadErrObj as Error).message : ''
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
    if (!selected || !desc.trim()) return
    setBusy(true)
    setError('')
    setApplied('')
    try {
      const r = await api.generateCapability({
        subscription,
        app,
        kind,
        triggerType,
        name: kind === 'custom_tool' || kind === 'skill' ? toolName : agentName,
        description: desc.trim(),
        groundInSkills: ground,
        foundry: selected.foundry,
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
        setApplied(`Saved a draft of ${path}.`)
      } else if (kind === 'skill') {
        const path = `skills/${skillSlug(toolName)}/SKILL.md`
        await api.saveSource({ subscription, app, path, content })
        qc.invalidateQueries({ queryKey: ['source', subscription, app, path] })
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
      <div className="field">
        <label>Foundry model</label>
        <SearchableSelect
          value={effectiveKey}
          onChange={setModelKey}
          options={options.map((o) => ({ value: o.key, label: o.label }))}
          placeholder={isLoading ? 'Loading Foundry models…' : 'No Foundry models in this subscription'}
          loading={isLoading}
          ariaLabel="Foundry model"
        />
        {loadErr && (
          <div className="hint" style={{ color: 'var(--red)' }}>
            {loadErr}
          </div>
        )}
      </div>
      {(kind === 'custom_tool' || kind === 'skill') && (
        <div className="field">
          <label>{kind === 'skill' ? 'Skill name *' : 'Tool name *'}</label>
          <input
            value={toolName}
            placeholder={kind === 'skill' ? 'api-assistant' : 'fetch_weather'}
            onChange={(e) => setToolName(e.target.value)}
          />
        </div>
      )}
      <div className="field">
        <label>Describe what to generate *</label>
        <textarea
          className="editor"
          style={{ minHeight: 84 }}
          spellCheck={false}
          value={desc}
          placeholder={
            kind === 'custom_tool'
              ? 'e.g. Fetch the current weather for a city from an HTTP API and return a short summary.'
              : kind === 'skill'
                ? 'e.g. How to query Azure Resource Graph: common KQL, endpoints, auth, and pitfalls.'
                : kind === 'connector_trigger'
                  ? 'e.g. When a new Outlook email arrives from my manager, draft a concise reply.'
                  : 'e.g. Accept a support ticket, classify urgency, and return a JSON triage result.'
          }
          onChange={(e) => setDesc(e.target.value)}
        />
      </div>
      <label className="check" style={{ fontSize: 12, marginBottom: 10 }}>
        <input type="checkbox" checked={ground} onChange={(e) => setGround(e.target.checked)} />
        Ground in this app's existing skills
      </label>
      <button
        className="btn primary"
        disabled={busy || !desc.trim() || !selected || ((kind === 'custom_tool' || kind === 'skill') && !toolName.trim())}
        onClick={() => void generate()}
      >
        {busy && !content ? (
          <>
            <span className="gh-spin" /> Generating…
          </>
        ) : (
          '✨ Generate'
        )}
      </button>
      {content && (
        <>
          <pre className="code" style={{ marginTop: 12, maxHeight: 320 }}>
            {content}
          </pre>
          <div className="gh-row">
            <button className="btn primary" disabled={busy} onClick={() => void apply()}>
              {busy
                ? 'Applying…'
                : kind === 'custom_tool'
                  ? 'Save as tool draft'
                  : kind === 'skill'
                    ? 'Save as skill draft'
                    : 'Apply to .agent.md draft'}
            </button>
            <button className="btn sm ghost" onClick={() => setContent('')}>
              Discard
            </button>
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
