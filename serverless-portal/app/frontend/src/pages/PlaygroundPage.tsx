import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, type LiveAgent } from '../api'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot, writeAgentsSnapshot } from '../query'

const enc = encodeURIComponent

type ChatMessage =
  | { role: 'user'; content: string }
  | { role: 'assistant'; content: string; toolCalls: Record<string, unknown>[] }
  | { role: 'error'; content: string }

function initials(name: string): string {
  const p = name.trim().split(/\s+/).filter(Boolean)
  return ((p[0]?.[0] ?? '?') + (p[1]?.[0] ?? '')).toUpperCase()
}

// Render a tool call from the chat response inline in the transcript. The shape
// varies by provider, so read common keys and fall back to a JSON dump.
function ToolCall({ call }: { call: Record<string, unknown> }) {
  const name =
    (typeof call.name === 'string' && call.name) ||
    (typeof call.tool === 'string' && call.tool) ||
    'tool'
  const args = call.arguments ?? call.args ?? call.input
  let detail = ''
  if (args !== undefined) {
    try {
      detail = typeof args === 'string' ? args : JSON.stringify(args, null, 2)
    } catch {
      detail = String(args)
    }
  }
  return (
    <div className="toolcall">
      <span className="t">▶ {name}</span>
      {detail && <pre>{detail}</pre>}
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
  const threadRef = useRef<HTMLDivElement>(null)

  // Switching agents starts a fresh conversation.
  useEffect(() => {
    setMessages([])
    setSessionId('')
  }, [selectedKey])

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight })
  }, [messages, sending])

  const send = async () => {
    const text = input.trim()
    if (!text || !agent || sending) return
    setInput('')
    setMessages((m) => [...m, { role: 'user', content: text }])
    setSending(true)
    try {
      const r = await api.agentChat({
        subscription: subForQuery,
        resourceGroup: agent.resourceGroup,
        app: agent.app,
        agent: agent.name,
        prompt: text,
        sessionId: sessionId || undefined,
      })
      if (r.sessionId) setSessionId(r.sessionId)
      setMessages((m) => [...m, { role: 'assistant', content: r.response, toolCalls: r.toolCalls }])
    } catch (e) {
      setMessages((m) => [...m, { role: 'error', content: (e as Error).message }])
    } finally {
      setSending(false)
    }
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
        <select
          style={{ width: 'auto' }}
          value={selectedKey}
          onChange={(e) => setSelectedKey(e.target.value)}
          disabled={!chatAgents.length}
          aria-label="Select agent"
        >
          {chatAgents.length === 0 && <option>No chat-enabled agents</option>}
          {chatAgents.map((a) => (
            <option key={keyOf(a)} value={keyOf(a)}>
              {a.name} · {a.app}
            </option>
          ))}
        </select>
        <label
          className="badge gray"
          style={{ cursor: 'not-allowed', opacity: 0.7 }}
          title="Streaming (SSE) is coming soon"
        >
          <input type="checkbox" disabled style={{ width: 'auto', marginRight: 6 }} /> Stream (soon)
        </label>
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
        <button
          className="btn sm"
          onClick={() => {
            setMessages([])
            setSessionId('')
          }}
          disabled={sending}
        >
          ＋ New session
        </button>
        <button className="btn sm" onClick={() => setMessages([])} disabled={sending || !messages.length}>
          Clear
        </button>
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
                ) : m.role === 'assistant' ? (
                  <div className="msg bot" key={i}>
                    <div className="av bot">✦</div>
                    <div>
                      {m.toolCalls?.map((c, j) => (
                        <ToolCall call={c} key={j} />
                      ))}
                      <div className="bubble">{m.content || '(no textual response)'}</div>
                    </div>
                  </div>
                ) : (
                  <div className="msg bot" key={i}>
                    <div className="av err">!</div>
                    <div className="bubble err">{m.content}</div>
                  </div>
                ),
              )}
              {sending && (
                <div className="msg bot">
                  <div className="av bot">✦</div>
                  <div className="bubble" style={{ color: 'var(--text-muted)' }}>
                    Thinking…
                  </div>
                </div>
              )}
            </div>
            <div className="composer">
              <input
                type="text"
                placeholder={`Message ${agent.name}…`}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    void send()
                  }
                }}
                disabled={sending}
              />
              <button
                className="btn primary"
                onClick={() => void send()}
                disabled={sending || !input.trim()}
              >
                {sending ? 'Sending…' : 'Send'}
              </button>
            </div>
          </div>

          <aside className="card trace-panel">
            <div className="card-head">
              <h3 style={{ margin: 0 }}>Live trace</h3>
              <span className="badge amber">soon</span>
            </div>
            <div className="trace-empty">
              <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
                Execution traces — model calls, tool &amp; MCP invocations, and timings — will stream here as
                the agent runs, once trace integration is enabled.
              </p>
              <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
                For now, any tool calls returned by the chat endpoint are shown inline in the transcript.
              </p>
            </div>
          </aside>
        </div>
      )}

      {agent && (
        <p className="hint">
          Calls <span className="mono">POST /agents/{agent.name}/chat</span> on{' '}
          <span className="mono">{agent.app}</span>, proxied via the portal (no key handling in the
          browser).
        </p>
      )}

      {isFetching && <p className="cache-stamp">⟳ Refreshing…</p>}
    </>
  )
}
