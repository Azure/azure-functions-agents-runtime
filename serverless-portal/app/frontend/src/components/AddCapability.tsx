// "Add capability" flow for the agent edit page — adds a trigger, a connector
// trigger, or an MCP tool server to an agent, writing the change into a portal
// draft of the agent's `.agent.md` or the app's `mcp.json`.

import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import {
  TRIGGER_SPECS,
  buildTriggerYaml,
  applyTriggerToMarkdown,
  addMcpServer,
  MCP_PRESETS,
  type McpServer,
} from '../capabilities'

type Mode = 'trigger' | 'connector' | 'mcp'

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

  const spec = TRIGGER_SPECS[trigType]
  const triggerIncomplete = spec.fields.some(
    (f) => f.required && !(trigValues[f.name] ?? f.default ?? '').trim(),
  )

  if (!open) {
    return (
      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-head">
          <h3 style={{ margin: 0 }}>➕ Add capability</h3>
          <button className="btn sm" onClick={() => setOpen(true)}>
            Add trigger, connector, or MCP tool
          </button>
        </div>
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          Add a trigger, a connector trigger, or an MCP tool. Changes are saved as a draft on this agent's{' '}
          <span className="mono">.agent.md</span> or the app's <span className="mono">mcp.json</span> — publish
          with <strong>Deploy edits</strong> or open a PR.
        </p>
      </div>
    )
  }

  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <div className="card-head">
        <h3 style={{ margin: 0 }}>➕ Add capability</h3>
        <button className="btn sm ghost" onClick={() => setOpen(false)}>
          Close
        </button>
      </div>

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
      </div>

      {mode === 'trigger' && (
        <>
          <div className="field">
            <label>Trigger type</label>
            <select
              value={trigType}
              onChange={(e) => {
                setTrigType(e.target.value)
                setTrigValues({})
                reset()
              }}
            >
              {Object.entries(TRIGGER_SPECS).map(([k, s]) => (
                <option key={k} value={k}>
                  {s.label}
                </option>
              ))}
            </select>
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
        </>
      )}

      {mode === 'connector' && (
        <>
          <p className="muted" style={{ fontSize: 13 }}>
            Sets a connector trigger (<span className="mono">generic_trigger · connectorTrigger</span>), so the
            agent runs when an Azure Connector event fires — e.g. a new Outlook email. Configure the connection
            in Azure, and add the matching connector MCP tool (e.g. Office 365) under the <strong>MCP tool</strong>{' '}
            tab.
          </p>
          <button className="btn primary" disabled={busy} onClick={() => void applyTrigger('connector')}>
            {busy ? 'Applying…' : 'Apply connector trigger'}
          </button>
        </>
      )}

      {mode === 'mcp' && (
        <>
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
    </div>
  )
}
