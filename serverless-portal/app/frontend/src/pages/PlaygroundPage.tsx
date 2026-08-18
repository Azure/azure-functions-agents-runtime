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
  const threadRef = useRef<HTMLDivElement>(null)

  // Switching agents starts a fresh conversation + trace.
  useEffect(() => {
    setMessages([])
    setSessionId('')
    setTrace([])
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
          const hint = /403|permission/i.test(raw)
            ? '\n\nThe app’s identity may not have access to the Foundry model. Use “Grant access” on the deploy result, or assign it the Cognitive Services User role on the Foundry account.'
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
        Home / <Link to={`/agents/${subForQuery}`}>Agents</Link> / Playground
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
            </div>
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
    </>
  )
}
