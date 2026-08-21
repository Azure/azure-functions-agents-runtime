import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, type LiveAgent } from '../api'
import { Button, Checkbox, Input } from '@coreai/fluentui-react'
import { SearchableSelect } from '../components/ui'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot, writeAgentsSnapshot } from '../query'

const enc = encodeURIComponent

type ChatMessage =
  | { role: 'user'; content: string }
  | { role: 'assistant'; content: string; isError?: boolean }

type SpanKind = 'model' | 'tool' | 'mcp'
interface TraceSpan {
  id: string
  kind: SpanKind
  name: string
  args?: unknown
  result?: unknown
  startMs: number
  endMs?: number
  status: 'running' | 'done' | 'error'
}

// Snapshot of the agent's resolved capabilities for a single turn, emitted by
// the runtime as a `capabilities` SSE event. Rendered at the top of the trace
// panel so users can distinguish "tool wasn't called" from "tool was denied by
// allow/deny policy".
interface CapabilitySnapshot {
  availableTools: string[]
  excluded: {
    userTools: string[]
    workflowTools: string[]
    mcpTools: string[]
  }
}

function initials(name: string): string {
  const p = name.trim().split(/\s+/).filter(Boolean)
  return ((p[0]?.[0] ?? '?') + (p[1]?.[0] ?? '')).toUpperCase()
}

function fmt(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

// Escape a string so it can be embedded inside single-quoted shell / Python.
function shq(s: string): string {
  return `'${s.replace(/'/g, "'\\''")}'`
}
function pyq(s: string): string {
  return `"${s.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
}

type Snippet = 'curl' | 'fetch' | 'python'

interface AgentEndpointCtx {
  host: string
  agentName: string
  prompt: string
  sessionId?: string
  stream: boolean
}

// Produce a snippet that calls the deployed agent's built-in chat endpoint
// directly. The function key is left as a placeholder — the value lives on the
// Azure portal side and is intentionally not proxied to the browser.
function buildSnippet(kind: Snippet, ctx: AgentEndpointCtx): string {
  const path = ctx.stream ? 'chatstream' : 'chat'
  const url = `https://${ctx.host}/agents/${encodeURIComponent(ctx.agentName)}/${path}`
  const body: Record<string, unknown> = { prompt: ctx.prompt || 'Hello!' }
  if (ctx.sessionId) body.session_id = ctx.sessionId
  const bodyJson = JSON.stringify(body, null, 2)
  if (kind === 'curl') {
    return [
      `curl -N -X POST ${shq(url)} \\`,
      `  -H ${shq('x-functions-key: <FUNCTION_KEY>')} \\`,
      `  -H ${shq('content-type: application/json')} \\`,
      ctx.stream ? `  -H ${shq('accept: text/event-stream')} \\` : '',
      `  --data ${shq(bodyJson)}`,
    ]
      .filter(Boolean)
      .join('\n')
  }
  if (kind === 'fetch') {
    return [
      `const res = await fetch(${JSON.stringify(url)}, {`,
      `  method: 'POST',`,
      `  headers: {`,
      `    'x-functions-key': '<FUNCTION_KEY>',`,
      `    'content-type': 'application/json',`,
      ctx.stream ? `    accept: 'text/event-stream',` : '',
      `  },`,
      `  body: ${JSON.stringify(bodyJson)},`,
      `})`,
      ctx.stream
        ? [
            `// Read the SSE stream — each frame is a JSON event.`,
            `const reader = res.body.getReader()`,
            `const decoder = new TextDecoder()`,
            `for (;;) {`,
            `  const { value, done } = await reader.read()`,
            `  if (done) break`,
            `  process.stdout.write(decoder.decode(value))`,
            `}`,
          ].join('\n')
        : `console.log(await res.json())`,
    ]
      .filter(Boolean)
      .join('\n')
  }
  // python
  return [
    `import httpx  # or requests`,
    ``,
    `resp = httpx.post(`,
    `    ${pyq(url)},`,
    `    headers={`,
    `        "x-functions-key": ${pyq('<FUNCTION_KEY>')},`,
    `        "content-type": "application/json",`,
    ctx.stream ? `        "accept": "text/event-stream",` : '',
    `    },`,
    `    json=${bodyJson.replace(/\btrue\b/g, 'True').replace(/\bfalse\b/g, 'False').replace(/\bnull\b/g, 'None')},`,
    `    timeout=60.0,`,
    `)`,
    ctx.stream ? `for chunk in resp.iter_lines():` : `print(resp.json())`,
    ctx.stream ? `    print(chunk)` : '',
  ]
    .filter(Boolean)
    .join('\n')
}

function CopyAsMenu({ ctx }: { ctx: AgentEndpointCtx }) {
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState<Snippet>('curl')
  const [copied, setCopied] = useState(false)
  const snippet = useMemo(() => buildSnippet(kind, ctx), [kind, ctx])
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(snippet)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard unavailable */
    }
  }
  return (
    <>
      <Button
        size="small"
        appearance="subtle"
        onClick={() => setOpen(true)}
        title="Copy the exact HTTP call this playground makes, as curl / fetch / Python"
      >
        ⇪ Copy as…
      </Button>
      {open && (
        <div className="copy-as-backdrop" onClick={() => setOpen(false)}>
          <div className="copy-as-card" onClick={(e) => e.stopPropagation()}>
            <div className="copy-as-head">
              <div className="copy-as-tabs" role="tablist" aria-label="Snippet format">
                {(['curl', 'fetch', 'python'] as Snippet[]).map((k) => (
                  <button
                    key={k}
                    type="button"
                    role="tab"
                    aria-selected={k === kind}
                    className={'copy-as-tab' + (k === kind ? ' is-active' : '')}
                    onClick={() => setKind(k)}
                  >
                    {k}
                  </button>
                ))}
              </div>
              <div style={{ flex: 1 }} />
              <Button size="small" appearance="primary" onClick={() => void copy()}>
                {copied ? 'Copied' : 'Copy'}
              </Button>
              <Button size="small" appearance="subtle" onClick={() => setOpen(false)}>
                Close
              </Button>
            </div>
            <p className="muted" style={{ fontSize: 12, margin: '0 0 8px' }}>
              Replace <span className="mono">&lt;FUNCTION_KEY&gt;</span> with the app’s host key from{' '}
              <em>App keys</em> in the Azure portal.
            </p>
            <pre className="code copy-as-snippet">{snippet}</pre>
          </div>
        </div>
      )}
    </>
  )
}

// Snapshot of the agent's resolved capabilities for the current turn. Rendered
// above the live trace so users can see at a glance which tools were denied
// by policy — those tools will never fire, but they're a common source of
// "why didn't the agent do X?" confusion.
function CapabilityStrip({ snapshot }: { snapshot: CapabilitySnapshot }) {
  const allExcluded = [
    ...snapshot.excluded.userTools.map((n) => ({ name: n, cat: 'tool' as const })),
    ...snapshot.excluded.workflowTools.map((n) => ({ name: n, cat: 'workflow' as const })),
    ...snapshot.excluded.mcpTools.map((n) => ({ name: n, cat: 'mcp' as const })),
  ]
  const nAvailable = snapshot.availableTools.length
  const nExcluded = allExcluded.length
  if (nAvailable === 0 && nExcluded === 0) return null
  return (
    <div className="cap-strip">
      <div className="cap-strip-head">
        <span className="cap-strip-title">Capabilities</span>
        <span className="cap-strip-meta">
          {nAvailable} available{nExcluded > 0 ? ` · ${nExcluded} denied` : ''}
        </span>
      </div>
      {snapshot.availableTools.length > 0 && (
        <div className="cap-strip-row">
          {snapshot.availableTools.map((n) => (
            <span className="cap-chip cap-chip-ok" key={`ok:${n}`} title={`${n} — available`}>
              {n}
            </span>
          ))}
        </div>
      )}
      {allExcluded.length > 0 && (
        <div className="cap-strip-row">
          {allExcluded.map(({ name, cat }) => (
            <span
              key={`deny:${cat}:${name}`}
              className={'cap-chip cap-chip-denied cap-chip-' + cat}
              title={`${name} — denied by ${cat === 'mcp' ? 'MCP' : cat} allow/deny policy`}
            >
              🚫 {name}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

// Session-history drawer for the current agent's app. Reads blob-backed
// sessions via the portal backend and lets the user replay a past
// conversation (loads the messages into the current thread and resumes the
// session id so the next turn continues where it left off).
function SessionsDrawer({
  open,
  onClose,
  agent,
  subscription,
  currentSessionId,
  onOpenSession,
}: {
  open: boolean
  onClose: () => void
  agent: LiveAgent | undefined
  subscription: string
  currentSessionId: string
  onOpenSession: (id: string, messages: ChatMessage[]) => void
}) {
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['sessions', subscription, agent?.resourceGroup ?? '', agent?.app ?? ''],
    queryFn: () =>
      api.listSessions({
        subscription,
        app: agent!.app,
        resourceGroup: agent!.resourceGroup,
      }),
    enabled: open && !!agent,
    staleTime: 30_000,
  })
  const [busy, setBusy] = useState('')
  const [errMsg, setErrMsg] = useState('')
  const openSession = async (id: string) => {
    if (!agent) return
    setBusy(id)
    setErrMsg('')
    try {
      const res = await api.getSession({
        subscription,
        app: agent.app,
        resourceGroup: agent.resourceGroup,
        sessionId: id,
      })
      const converted: ChatMessage[] = []
      for (const m of res.messages) {
        const role = String(m.role ?? '').toLowerCase()
        const content = typeof m.content === 'string' ? m.content : ''
        if (!content) continue
        if (role === 'user') converted.push({ role: 'user', content })
        else converted.push({ role: 'assistant', content })
      }
      onOpenSession(id, converted)
      onClose()
    } catch (e) {
      setErrMsg((e as Error).message)
    } finally {
      setBusy('')
    }
  }
  if (!open) return null
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h3 style={{ margin: 0 }}>Sessions</h3>
          <div style={{ flex: 1 }} />
          <Button size="small" appearance="subtle" onClick={() => void refetch()} disabled={isFetching}>
            {isFetching ? '…' : 'Refresh'}
          </Button>
          <Button size="small" appearance="subtle" onClick={onClose}>
            Close
          </Button>
        </div>
        <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          Persisted conversations on{' '}
          <span className="mono">{agent?.app ?? ''}</span>'s storage. Sessions are shared across every agent in
          the app.
        </p>
        {isLoading && <div className="muted">Loading…</div>}
        {error && (
          <div className="gh-err" style={{ marginTop: 8 }}>
            {(error as Error).message}
          </div>
        )}
        {errMsg && (
          <div className="gh-err" style={{ marginTop: 8 }}>
            {errMsg}
          </div>
        )}
        {data && data.readable === false && (
          <div className="note" style={{ marginTop: 8 }}>
            Storage isn't readable with the current identity. Grant "Storage Blob Data Reader" on the app's
            storage account to browse sessions.
          </div>
        )}
        {data && data.sessions.length === 0 && data.readable && (
          <div className="muted" style={{ fontSize: 13, marginTop: 8 }}>
            No sessions yet. Start a chat here — the runtime persists every turn to blob storage.
          </div>
        )}
        <ul className="session-list">
          {data?.sessions.map((s) => (
            <li key={s.sessionId} className={'session-row' + (s.sessionId === currentSessionId ? ' is-current' : '')}>
              <button
                type="button"
                className="session-open"
                onClick={() => void openSession(s.sessionId)}
                disabled={busy === s.sessionId}
                title={`Replay ${s.sessionId}`}
              >
                <span className="session-id mono">{s.sessionId.slice(0, 24)}</span>
                <span className="session-meta">
                  {s.lastModified ? new Date(s.lastModified).toLocaleString() : '—'}
                  {' · '}
                  {(s.size / 1024).toFixed(1)} KB
                </span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
    </div>
  )
}

function TraceSpanRow({ span }: { span: TraceSpan }) {
  const [open, setOpen] = useState(false)
  const hasDetail = span.args !== undefined || span.result !== undefined
  const dur =
    span.endMs != null ? `${Math.max(0, span.endMs - span.startMs)} ms` : span.status === 'running' ? '…' : ''
  return (
    <div className={'span ' + span.status}>
      <button
        type="button"
        className="span-head"
        onClick={() => hasDetail && setOpen((o) => !o)}
        disabled={!hasDetail}
        title={hasDetail ? 'Show details' : undefined}
      >
        <span className={'span-kind k-' + span.kind}>{span.kind}</span>
        <span className="span-name mono">{span.name}</span>
        <span className={'span-dot ' + span.status} />
        <span className="span-dur">{dur}</span>
      </button>
      {open && hasDetail && (
        <div className="span-detail">
          {span.args !== undefined && (
            <>
              <div className="span-lbl">arguments</div>
              <pre>{fmt(span.args)}</pre>
            </>
          )}
          {span.result !== undefined && (
            <>
              <div className="span-lbl">result</div>
              <pre>{fmt(span.result)}</pre>
            </>
          )}
        </div>
      )}
    </div>
  )
}

export default function PlaygroundPage() {
  const { subscriptionId, app: appParam, name: nameParam } = useParams<{
    subscriptionId: string
    app: string
    name: string
  }>()
  const { selected, setSelected, identity } = useIdentity()

  // Deeplink → adopt the subscription from the URL.
  useEffect(() => {
    if (subscriptionId && subscriptionId !== selected) setSelected(subscriptionId)
  }, [subscriptionId, selected, setSelected])

  const subForQuery = subscriptionId || selected
  const snapshot = useMemo(() => readAgentsSnapshot(subForQuery), [subForQuery])
  const { data, isFetching, dataUpdatedAt } = useQuery({
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
    if (subForQuery && data) writeAgentsSnapshot(subForQuery, data, dataUpdatedAt)
  }, [subForQuery, data, dataUpdatedAt])

  // Only agents that expose a built-in chat endpoint can be run here.
  const chatAgents = useMemo(() => (data?.agents ?? []).filter((a) => a.builtinEndpoints), [data])
  const keyOf = (a: { app: string; name: string }) => `${a.app}::${a.name}`
  const [selectedKey, setSelectedKey] = useState('')

  // Select the deep-linked agent, else the first chat-capable one.
  useEffect(() => {
    if (!chatAgents.length) return
    const deep = appParam && nameParam ? `${appParam}::${nameParam}` : ''
    if (!chatAgents.some((a) => keyOf(a) === selectedKey)) {
      setSelectedKey(chatAgents.some((a) => keyOf(a) === deep) ? deep : keyOf(chatAgents[0]))
    }
  }, [chatAgents, appParam, nameParam, selectedKey])

  const agent: LiveAgent | undefined = useMemo(
    () => chatAgents.find((a) => keyOf(a) === selectedKey),
    [chatAgents, selectedKey],
  )

  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [sending, setSending] = useState(false)
  const [stream, setStream] = useState(true)
  const [trace, setTrace] = useState<TraceSpan[]>([])
  const [capabilities, setCapabilities] = useState<CapabilitySnapshot | null>(null)
  const [sessionsOpen, setSessionsOpen] = useState(false)
  const [compareOpen, setCompareOpen] = useState(false)
  const [compareKey, setCompareKey] = useState('')
  const [hitPermissionError, setHitPermissionError] = useState(false)
  const [grantBusy, setGrantBusy] = useState(false)
  const [grantMsg, setGrantMsg] = useState('')
  const threadRef = useRef<HTMLDivElement>(null)

  const runGrantAccess = async () => {
    if (!agent || grantBusy) return
    setGrantBusy(true)
    setGrantMsg('')
    try {
      const r = await api.healFoundryAccess({
        subscription: subForQuery,
        resourceGroup: agent.resourceGroup,
        app: agent.app,
      })
      if (r.granted.length && r.failed.length === 0) {
        setGrantMsg(`✓ Granted ${r.granted.join(', ')} on ${r.account}. Retry the prompt now.`)
        setHitPermissionError(false)
      } else if (r.granted.length) {
        setGrantMsg(`Partial: granted ${r.granted.join(', ')}; failed ${r.failed.map((f) => f.role).join(', ')}.`)
      } else {
        setGrantMsg(`⚠ Grant failed: ${r.failed.map((f) => f.error).join(' · ')}`)
      }
    } catch (e) {
      setGrantMsg(`⚠ ${(e as Error).message}`)
    } finally {
      setGrantBusy(false)
    }
  }

  // Switching agents starts a fresh conversation + trace.
  useEffect(() => {
    setMessages([])
    setSessionId('')
    setTrace([])
    setCapabilities(null)
  }, [selectedKey])

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight })
  }, [messages, sending, trace])

  // Patch the most recent assistant message (the one being streamed into).
  const patchAssistant = (patch: Partial<Extract<ChatMessage, { role: 'assistant' }>>) => {
    setMessages((m) => {
      const copy = [...m]
      for (let i = copy.length - 1; i >= 0; i--) {
        if (copy[i].role === 'assistant') {
          copy[i] = { ...(copy[i] as Extract<ChatMessage, { role: 'assistant' }>), ...patch }
          break
        }
      }
      return copy
    })
  }

  const runStreaming = async (text: string, a: LiveAgent) => {
    const t0 = performance.now()
    const now = () => Math.round(performance.now() - t0)
    setTrace([{ id: 'model', kind: 'model', name: a.provider || 'model', startMs: 0, status: 'running' }])

    const upsertSpan = (id: string, patch: Partial<TraceSpan>) => {
      setTrace((prev) => {
        const i = prev.findIndex((s) => s.id === id)
        if (i === -1) {
          return [
            ...prev,
            {
              id,
              kind: patch.kind ?? 'tool',
              name: patch.name ?? id,
              startMs: patch.startMs ?? now(),
              status: 'running',
              ...patch,
            },
          ]
        }
        const copy = [...prev]
        copy[i] = { ...copy[i], ...patch }
        return copy
      })
    }
    const finishModel = () =>
      setTrace((prev) =>
        prev.map((s) => (s.id === 'model' && s.status === 'running' ? { ...s, endMs: now(), status: 'done' } : s)),
      )

    let streamText = ''
    let sawError = false
    const handle = (evt: Record<string, unknown>) => {
      switch (evt.type) {
        case 'session':
          if (typeof evt.session_id === 'string' && evt.session_id) setSessionId(evt.session_id)
          break
        case 'capabilities': {
          const excluded = (evt.excluded ?? {}) as Record<string, unknown>
          const toArr = (v: unknown): string[] =>
            Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && !!x) : []
          setCapabilities({
            availableTools: toArr(evt.available_tools),
            excluded: {
              userTools: toArr(excluded.user_tools),
              workflowTools: toArr(excluded.workflow_tools),
              mcpTools: toArr(excluded.mcp_tools),
            },
          })
          break
        }
        case 'delta':
        case 'message':
          if (typeof evt.content === 'string' && evt.content) {
            streamText = evt.type === 'message' ? evt.content : streamText + evt.content
            patchAssistant({ content: streamText })
          }
          break
        case 'tool_start': {
          const name = String(evt.tool_name || 'tool')
          const id = String(evt.tool_call_id || evt.event_id || `${name}-${now()}`)
          upsertSpan(id, {
            kind: /mcp/i.test(name) ? 'mcp' : 'tool',
            name,
            args: evt.arguments,
            startMs: now(),
            status: 'running',
          })
          break
        }
        case 'tool_end': {
          const id = String(evt.tool_call_id || evt.event_id || '')
          if (id)
            upsertSpan(id, {
              result: evt.result,
              endMs: now(),
              status: 'done',
              name: String(evt.tool_name || '') || undefined,
            })
          break
        }
        case 'error': {
          finishModel()
          sawError = true
          const raw = String(evt.content || 'Agent error')
          const isPerm = /403|permission|cognitive/i.test(raw)
          if (isPerm) setHitPermissionError(true)
          const hint = isPerm
            ? '\n\nThe app’s identity may not have access to the Foundry model. Click "Grant access" below to assign the Cognitive Services User role, then retry.'
            : ''
          patchAssistant({
            content: (streamText ? streamText + '\n\n' : '') + '⚠ ' + raw + hint,
            isError: true,
          })
          break
        }
        case 'done':
          finishModel()
          break
      }
    }

    const res = await api.agentChatStream({
      subscription: subForQuery,
      resourceGroup: a.resourceGroup,
      app: a.app,
      agent: a.name,
      prompt: text,
      sessionId: sessionId || undefined,
    })
    const reader = res.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        for (const ln of frame.split('\n')) {
          const s = ln.replace(/^\s+/, '')
          if (!s.startsWith('data:')) continue
          const json = s.slice(5).trim()
          if (!json) continue
          try {
            handle(JSON.parse(json))
          } catch {
            /* ignore a partial/non-JSON frame */
          }
        }
      }
    }
    finishModel()
    if (!streamText && !sawError) patchAssistant({ content: '(no textual response)' })
  }

  const runOnce = async (text: string, a: LiveAgent) => {
    setTrace([])
    const r = await api.agentChat({
      subscription: subForQuery,
      resourceGroup: a.resourceGroup,
      app: a.app,
      agent: a.name,
      prompt: text,
      sessionId: sessionId || undefined,
    })
    if (r.sessionId) setSessionId(r.sessionId)
    patchAssistant({ content: r.response || '(no textual response)' })
    setTrace(
      r.toolCalls.map((c, i) => {
        const rec = c as Record<string, unknown>
        const name = String(rec.tool_name ?? 'tool')
        return {
          id: String(rec.tool_call_id ?? i),
          kind: /mcp/i.test(name) ? 'mcp' : 'tool',
          name,
          args: rec.arguments,
          result: rec.result,
          startMs: 0,
          status: 'done' as const,
        }
      }),
    )
  }

  const send = async () => {
    const text = input.trim()
    if (!text || !agent || sending) return
    setInput('')
    setMessages((m) => [...m, { role: 'user', content: text }, { role: 'assistant', content: '' }])
    setSending(true)
    try {
      if (stream) await runStreaming(text, agent)
      else await runOnce(text, agent)
    } catch (e) {
      patchAssistant({ content: (e as Error).message, isError: true })
      setTrace((prev) => prev.map((s) => (s.status === 'running' ? { ...s, status: 'error' } : s)))
    } finally {
      setSending(false)
    }
  }

  const clearAll = () => {
    setMessages([])
    setTrace([])
  }
  const newSession = () => {
    setMessages([])
    setTrace([])
    setSessionId('')
  }

  const you = initials(identity?.user?.name || identity?.user?.username || 'You')

  return (
    <>
      <div className="breadcrumb">
        Home / <Link to={`/agents/${subForQuery}`}>Hosted Skills</Link> / Playground
      </div>
      <div className="page-title">
        <h1>Playground</h1>
      </div>
      <p className="page-sub">Run a deployed agent by chatting with its built-in endpoint.</p>

      <div className="toolbar">
        <SearchableSelect
          value={selectedKey}
          onChange={setSelectedKey}
          options={chatAgents.map((a) => ({ value: keyOf(a), label: `${a.name} · ${a.app}` }))}
          placeholder={chatAgents.length ? 'Select an agent…' : 'No chat-enabled agents'}
          disabled={!chatAgents.length}
          ariaLabel="Select agent"
        />
        <Checkbox
          checked={stream}
          onChange={(_, data) => setStream(data.checked === true)}
          label="Stream"
          title="Stream tokens + live trace"
        />
        {sessionId && (
          <span className="badge blue">
            <span className="dot" /> session <span className="mono">{sessionId.slice(0, 8)}…</span>
          </span>
        )}
        <div style={{ flex: 1 }} />
        {agent && (
          <Link className="btn sm" to={`/agents/${subForQuery}/${enc(agent.app)}/${enc(agent.name)}`}>
            Open agent
          </Link>
        )}
        {agent && (
          <Button
            size="small"
            appearance={sessionsOpen ? 'primary' : 'secondary'}
            onClick={() => setSessionsOpen((v) => !v)}
            title="Browse this app's persisted conversation history"
          >
            🕓 Sessions
          </Button>
        )}
        {chatAgents.length > 1 && (
          <Button
            size="small"
            appearance={compareOpen ? 'primary' : 'secondary'}
            onClick={() => {
              setCompareOpen((v) => !v)
              if (!compareOpen && !compareKey) {
                const other = chatAgents.find((a) => keyOf(a) !== selectedKey)
                if (other) setCompareKey(keyOf(other))
              }
            }}
            title="Send the next prompt to two agents in parallel and diff the responses"
          >
            ⇔ Compare
          </Button>
        )}
        <Button size="small" onClick={newSession} disabled={sending}>
          ＋ New session
        </Button>
        <Button size="small" onClick={clearAll} disabled={sending || !messages.length}>
          Clear
        </Button>
      </div>

      {!subForQuery && <div className="empty">Select a subscription to load agents.</div>}
      {subForQuery && !chatAgents.length && (
        <div className="empty">
          {isFetching
            ? 'Loading agents…'
            : 'No agents with built-in chat endpoints in this subscription. Enable built-in endpoints on an agent to run it here.'}
        </div>
      )}

      {agent && (
        <div className="playground">
          <div className="card chat">
            <div className="thread" ref={threadRef}>
              {messages.length === 0 && (
                <div className="chat-empty">
                  <div className="chat-empty-mark">💬</div>
                  Ask <span className="mono">{agent.name}</span> anything to start.
                </div>
              )}
              {messages.map((m, i) =>
                m.role === 'user' ? (
                  <div className="msg user" key={i}>
                    <div className="av user">{you}</div>
                    <div className="bubble">{m.content}</div>
                  </div>
                ) : (
                  <div className="msg bot" key={i}>
                    <div className={'av ' + (m.isError ? 'err' : 'bot')}>{m.isError ? '!' : '✦'}</div>
                    <div className={'bubble' + (m.isError ? ' err' : '')}>
                      {m.content ||
                        (sending && i === messages.length - 1 ? (
                          <span style={{ color: 'var(--text-muted)' }}>Thinking…</span>
                        ) : (
                          ''
                        ))}
                    </div>
                  </div>
                ),
              )}
            </div>
            <div className="composer">
              <Input
                type="text"
                placeholder={`Message ${agent.name}…`}
                value={input}
                onChange={(_, data) => setInput(data.value)}
                disabled={sending}
                input={{
                  onKeyDown: (e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      void send()
                    }
                  },
                }}
              />
              <Button appearance="primary" onClick={() => void send()} disabled={sending || !input.trim()}>
                {sending ? 'Sending…' : 'Send'}
              </Button>
              <CopyAsMenu
                ctx={{
                  host: agent.defaultHostName,
                  agentName: agent.name,
                  prompt: input,
                  sessionId: sessionId || undefined,
                  stream,
                }}
              />
            </div>
            {hitPermissionError && (
              <div className="note" style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 10 }}>
                <span>Foundry permission error detected. Grant this app the roles it needs to call the model:</span>
                <div style={{ flex: 1 }} />
                <Button
                  appearance="primary"
                  size="small"
                  onClick={() => void runGrantAccess()}
                  disabled={grantBusy}
                >
                  {grantBusy ? 'Granting…' : '🔑 Grant access'}
                </Button>
              </div>
            )}
            {grantMsg && (
              <div
                className={'note' + (grantMsg.startsWith('✓') ? ' ok' : '')}
                style={{ marginTop: 10 }}
              >
                {grantMsg}
              </div>
            )}
          </div>

          <aside className="card trace-panel">
            <div className="card-head">
              <h3 style={{ margin: 0 }}>Live trace</h3>
              {trace.length > 0 && (
                <span className="badge gray">
                  {trace.length} span{trace.length > 1 ? 's' : ''}
                </span>
              )}
            </div>
            {capabilities && <CapabilityStrip snapshot={capabilities} />}
            {trace.length === 0 ? (
              <div className="trace-empty">
                <p className="muted" style={{ fontSize: 13, marginTop: 0, marginBottom: 0 }}>
                  {stream
                    ? 'Model, tool, and MCP spans stream here as the agent runs. Click a span for its arguments and result.'
                    : 'Turn on Stream to see live execution spans and timings.'}
                </p>
              </div>
            ) : (
              <div className="trace">
                {trace.map((s) => (
                  <TraceSpanRow key={s.id} span={s} />
                ))}
              </div>
            )}
          </aside>
        </div>
      )}

      {agent && (
        <p className="hint">
          Calls{' '}
          <span className="mono">
            POST /agents/{agent.name}/{stream ? 'chatstream' : 'chat'}
          </span>{' '}
          on <span className="mono">{agent.app}</span>, proxied via the portal. Tool &amp; MCP calls appear
          in the live trace.
        </p>
      )}

      {isFetching && <p className="cache-stamp">⟳ Refreshing…</p>}

      <SessionsDrawer
        open={sessionsOpen}
        onClose={() => setSessionsOpen(false)}
        agent={agent}
        subscription={subForQuery}
        currentSessionId={sessionId}
        onOpenSession={(id, msgs) => {
          setSessionId(id)
          setMessages(msgs)
          setTrace([])
          setCapabilities(null)
        }}
      />

      {compareOpen && agent && (
        <ComparePanel
          subscription={subForQuery}
          leftAgent={agent}
          agents={chatAgents}
          rightAgentKey={compareKey}
          setRightAgentKey={setCompareKey}
          keyOf={keyOf}
          onClose={() => setCompareOpen(false)}
        />
      )}
    </>
  )
}

// Side-by-side compare mode — sends a single prompt to two agents in parallel
// and shows their responses next to each other. Uses the non-streaming
// endpoint so both threads finish before the diff is meaningful. Each agent
// gets its own session id (isolated conversation) so back-and-forth compare
// keeps working.
function ComparePanel({
  subscription,
  leftAgent,
  agents,
  rightAgentKey,
  setRightAgentKey,
  keyOf,
  onClose,
}: {
  subscription: string
  leftAgent: LiveAgent
  agents: LiveAgent[]
  rightAgentKey: string
  setRightAgentKey: (v: string) => void
  keyOf: (a: { app: string; name: string }) => string
  onClose: () => void
}) {
  const rightAgent = agents.find((a) => keyOf(a) === rightAgentKey)
  const [prompt, setPrompt] = useState('')
  const [busy, setBusy] = useState(false)
  const [leftResp, setLeftResp] = useState<{ content: string; ms: number; err?: string } | null>(null)
  const [rightResp, setRightResp] = useState<{ content: string; ms: number; err?: string } | null>(null)
  const [leftSess, setLeftSess] = useState('')
  const [rightSess, setRightSess] = useState('')

  const runOne = async (a: LiveAgent, sess: string, setSess: (s: string) => void) => {
    const t0 = performance.now()
    try {
      const r = await api.agentChat({
        subscription,
        resourceGroup: a.resourceGroup,
        app: a.app,
        agent: a.name,
        prompt: prompt.trim(),
        sessionId: sess || undefined,
      })
      if (r.sessionId) setSess(r.sessionId)
      return { content: r.response || '(no textual response)', ms: Math.round(performance.now() - t0) }
    } catch (e) {
      return { content: '', ms: Math.round(performance.now() - t0), err: (e as Error).message }
    }
  }

  const send = async () => {
    if (!prompt.trim() || !rightAgent || busy) return
    setBusy(true)
    setLeftResp(null)
    setRightResp(null)
    try {
      const [l, r] = await Promise.all([
        runOne(leftAgent, leftSess, setLeftSess),
        runOne(rightAgent, rightSess, setRightSess),
      ])
      setLeftResp(l)
      setRightResp(r)
    } finally {
      setBusy(false)
    }
  }

  const reset = () => {
    setPrompt('')
    setLeftResp(null)
    setRightResp(null)
    setLeftSess('')
    setRightSess('')
  }

  const options = agents.map((a) => ({ value: keyOf(a), label: `${a.name} · ${a.app}` }))

  return (
    <section className="card compare-panel" style={{ marginTop: 18 }}>
      <div className="card-head">
        <h3 style={{ margin: 0 }}>Compare</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          One prompt → two agents. Session ids are independent so follow-ups keep working.
        </span>
        <div style={{ flex: 1 }} />
        <Button size="small" appearance="subtle" onClick={reset} disabled={busy}>
          Reset
        </Button>
        <Button size="small" appearance="subtle" onClick={onClose}>
          Close
        </Button>
      </div>
      <div className="compare-grid">
        <div className="field">
          <label>Left</label>
          <div className="ss-trigger is-open" style={{ cursor: 'default' }}>
            <span className="ss-trigger-label mono">{leftAgent.name}</span>
          </div>
          <div className="hint">
            <span className="mono">{leftAgent.app}</span> · session{' '}
            {leftSess ? <span className="mono">{leftSess.slice(0, 8)}…</span> : 'new'}
          </div>
        </div>
        <div className="field">
          <label>Right</label>
          <SearchableSelect
            value={rightAgentKey}
            onChange={setRightAgentKey}
            options={options}
            placeholder="Choose an agent to compare against"
            ariaLabel="Right agent"
          />
          <div className="hint">
            {rightAgent ? (
              <>
                <span className="mono">{rightAgent.app}</span> · session{' '}
                {rightSess ? <span className="mono">{rightSess.slice(0, 8)}…</span> : 'new'}
              </>
            ) : (
              '—'
            )}
          </div>
        </div>
      </div>
      <div className="composer" style={{ marginTop: 12 }}>
        <Input
          type="text"
          placeholder={rightAgent ? 'Send the same prompt to both agents…' : 'Pick a right-hand agent first'}
          value={prompt}
          onChange={(_, data) => setPrompt(data.value)}
          disabled={busy || !rightAgent}
          input={{
            onKeyDown: (e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                void send()
              }
            },
          }}
        />
        <Button appearance="primary" onClick={() => void send()} disabled={busy || !prompt.trim() || !rightAgent}>
          {busy ? 'Sending…' : 'Send to both'}
        </Button>
      </div>
      <div className="compare-grid" style={{ marginTop: 12 }}>
        <ComparePane label={leftAgent.name} response={leftResp} busy={busy} />
        <ComparePane label={rightAgent?.name ?? '—'} response={rightResp} busy={busy} />
      </div>
    </section>
  )
}

function ComparePane({
  label,
  response,
  busy,
}: {
  label: string
  response: { content: string; ms: number; err?: string } | null
  busy: boolean
}) {
  return (
    <div className="compare-pane">
      <div className="compare-pane-head">
        <span className="mono">{label}</span>
        {response && !response.err && <span className="badge gray">{response.ms} ms</span>}
        {response?.err && <span className="badge red">error</span>}
      </div>
      <div className={'compare-pane-body' + (response?.err ? ' is-error' : '')}>
        {busy && !response && <span className="muted">Thinking…</span>}
        {response?.err ? (
          <pre className="mono" style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
            ⚠ {response.err}
          </pre>
        ) : (
          <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontFamily: 'inherit', fontSize: 13.5 }}>
            {response?.content ?? ''}
          </pre>
        )}
      </div>
    </div>
  )
}
