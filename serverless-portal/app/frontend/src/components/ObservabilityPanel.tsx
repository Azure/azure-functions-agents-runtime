import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Button } from '@coreai/fluentui-react'
import { api, type AppInsightsResult } from '../api'

type TimeRange = '1h' | '24h'
type RangeMode = TimeRange | 'custom'

interface InvocationSpan {
  startTime: string
  traceId: string
  spanId: string
  parentSpanId: string
  name: string
  kind: string
  durationMs: number
  statusCode: string
  attributes: Record<string, unknown>
}

interface TraceSpan {
  startTime: string
  itemType: string
  name: string
  spanId: string
  parentSpanId: string
  durationMs: number
  statusCode: string
  attributes: Record<string, unknown>
}

interface InvocationPage {
  spans: InvocationSpan[]
  hasMore: boolean
}

function parseAttributes(value: unknown): Record<string, unknown> {
  if (value && typeof value === 'object' && !Array.isArray(value)) return value as Record<string, unknown>
  if (typeof value !== 'string' || !value.trim()) return {}
  try {
    const parsed = JSON.parse(value) as unknown
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {}
  } catch {
    return {}
  }
}

function rowsOf(result?: AppInsightsResult): InvocationPage {
  const table = result?.tables?.[0]
  if (!table || !Array.isArray(table.columns) || !Array.isArray(table.rows)) return { spans: [], hasMore: false }
  const index = new Map(table.columns.map((column, position) => [column.name, position]))
  const value = (row: unknown[], name: string) => row[index.get(name) ?? -1]
  const spans = table.rows.map((row) => ({
    startTime: String(value(row, 'start_time') ?? ''),
    traceId: String(value(row, 'trace_id') ?? ''),
    spanId: String(value(row, 'span_id') ?? ''),
    parentSpanId: String(value(row, 'parent_span_id') ?? ''),
    name: String(value(row, 'span_name') ?? ''),
    kind: String(value(row, 'span_kind') ?? 'SPAN_KIND_INTERNAL'),
    durationMs: Number(value(row, 'duration_ms') ?? 0),
    statusCode: String(value(row, 'status_code') ?? 'STATUS_CODE_UNSET'),
    attributes: parseAttributes(value(row, 'attributes')),
  }))
  const firstRow = table.rows[0]
  return {
    spans,
    hasMore: firstRow ? String(value(firstRow, 'has_more') ?? 'false').toLowerCase() === 'true' : false,
  }
}

function traceRowsOf(result?: AppInsightsResult): TraceSpan[] {
  const table = result?.tables?.[0]
  if (!table || !Array.isArray(table.columns) || !Array.isArray(table.rows)) return []
  const index = new Map(table.columns.map((column, position) => [column.name, position]))
  const value = (row: unknown[], name: string) => row[index.get(name) ?? -1]
  return table.rows.map((row) => ({
    startTime: String(value(row, 'start_time') ?? ''),
    itemType: String(value(row, 'item_type') ?? 'dependency'),
    name: String(value(row, 'span_name') ?? ''),
    spanId: String(value(row, 'span_id') ?? ''),
    parentSpanId: String(value(row, 'parent_span_id') ?? ''),
    durationMs: Number(value(row, 'duration_ms') ?? 0),
    statusCode: String(value(row, 'status_code') ?? 'STATUS_CODE_UNSET'),
    attributes: parseAttributes(value(row, 'attributes')),
  }))
}

function otelValue(value: unknown): Record<string, unknown> {
  if (typeof value === 'boolean') return { boolValue: value }
  if (typeof value === 'number') return Number.isInteger(value) ? { intValue: String(value) } : { doubleValue: value }
  return { stringValue: typeof value === 'string' ? value : JSON.stringify(value) }
}

function asOtlpSpan(span: InvocationSpan): Record<string, unknown> {
  const startMs = Date.parse(span.startTime)
  const startNanos = Number.isFinite(startMs) ? BigInt(startMs) * 1_000_000n : 0n
  const endNanos = startNanos + BigInt(Math.max(0, Math.round(span.durationMs * 1_000_000)))
  return {
    traceId: span.traceId,
    spanId: span.spanId,
    parentSpanId: span.parentSpanId,
    name: span.name,
    kind: span.kind,
    startTimeUnixNano: startNanos.toString(),
    endTimeUnixNano: endNanos.toString(),
    attributes: Object.entries(span.attributes).map(([key, value]) => ({ key, value: otelValue(value) })),
    status: { code: span.statusCode },
  }
}

function formatDuration(durationMs: number): string {
  if (durationMs < 1_000) return `${Math.round(durationMs)} ms`
  return `${(durationMs / 1_000).toFixed(2)} s`
}

function InvocationTrace({ open, invocation, subscription, resourceGroup, app, timeRange, startTime, endTime }: {
  open: boolean
  invocation: InvocationSpan
  subscription: string
  resourceGroup: string
  app: string
  timeRange: TimeRange
  startTime?: string
  endTime?: string
}) {
  const query = useQuery({
    queryKey: ['ai:trace', subscription, resourceGroup, app, invocation.traceId, timeRange, startTime, endTime],
    queryFn: () => api.appInsightsQuery({
      subscription,
      resourceGroup,
      app,
      traceId: invocation.traceId,
      preset: 'trace',
      timeRange,
      startTime,
      endTime,
    }),
    enabled: open,
    staleTime: 5 * 60_000,
    retry: false,
  })
  const spans = traceRowsOf(query.data)
  const starts = spans.map((span) => Date.parse(span.startTime)).filter(Number.isFinite)
  const origin = starts.length ? Math.min(...starts) : 0
  const finish = spans.reduce((latest, span) => Math.max(latest, Date.parse(span.startTime) + span.durationMs), origin)
  const totalDuration = Math.max(1, finish - origin)
  const spanById = new Map(spans.map((span) => [span.spanId, span]))
  const depthOf = (span: TraceSpan): number => {
    let depth = 0
    let parent = spanById.get(span.parentSpanId)
    const visited = new Set<string>()
    while (parent && depth < 8 && !visited.has(parent.spanId)) {
      visited.add(parent.spanId)
      depth += 1
      parent = spanById.get(parent.parentSpanId)
    }
    return depth
  }

  if (!open) return null
  if (query.isLoading) return <div className="otel-trace-loading"><div className="skeleton shimmer-bar" />Loading transaction spans…</div>
  if (query.error) return <div className="note warn">Couldn’t load transaction details: {(query.error as Error).message}</div>
  if (!spans.length) return <div className="note">No correlated child spans were found for this trace.</div>

  return (
    <div className="otel-transaction">
      <div className="otel-transaction-head">
        <div><strong>End-to-end transaction</strong><span>{spans.length} spans</span></div>
        <span>{formatDuration(totalDuration)}</span>
      </div>
      <div className="otel-waterfall" role="tree" aria-label="OpenTelemetry transaction spans">
        {spans.map((span, index) => {
          const start = Date.parse(span.startTime)
          const left = Number.isFinite(start) ? ((start - origin) / totalDuration) * 100 : 0
          const width = Math.max(0.8, (span.durationMs / totalDuration) * 100)
          return (
            <details className="otel-waterfall-row" key={`${span.spanId}:${index}`} role="treeitem">
              <summary>
                <span className={'otel-status ' + (span.statusCode === 'STATUS_CODE_ERROR' ? 'error' : 'ok')} />
                <span className="otel-waterfall-name" style={{ paddingLeft: `${depthOf(span) * 14}px` }}>
                  <strong>{span.name}</strong><small>{span.itemType}</small>
                </span>
                <span className="otel-waterfall-track" aria-label={`Started ${Math.max(0, start - origin).toFixed(1)} milliseconds after trace start`}>
                  <span style={{ left: `${Math.min(99.2, left)}%`, width: `${Math.min(100 - left, width)}%` }} />
                </span>
                <span className="otel-waterfall-duration">{formatDuration(span.durationMs)}</span>
              </summary>
              <div className="otel-waterfall-attributes">
                <span><strong>span_id</strong> <code>{span.spanId}</code></span>
                <span><strong>parent_span_id</strong> <code>{span.parentSpanId || '—'}</code></span>
                <pre>{JSON.stringify(span.attributes, null, 2)}</pre>
              </div>
            </details>
          )
        })}
      </div>
      <details className="otel-raw-envelope">
        <summary>Raw OTLP invocation envelope</summary>
        <pre>{JSON.stringify(asOtlpSpan(invocation), null, 2)}</pre>
      </details>
    </div>
  )
}

function InvocationRow({ span, subscription, resourceGroup, app, timeRange, startTime, endTime }: {
  span: InvocationSpan
  subscription: string
  resourceGroup: string
  app: string
  timeRange: TimeRange
  startTime?: string
  endTime?: string
}) {
  const [open, setOpen] = useState(false)
  return (
    <details className="otel-span" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>
        <span className={'otel-status ' + (span.statusCode === 'STATUS_CODE_ERROR' ? 'error' : 'ok')} />
        <span><strong>{span.name}</strong><small>{new Date(span.startTime).toLocaleString()}</small></span>
        <code>{span.traceId.slice(0, 12)}</code>
        <span>{formatDuration(span.durationMs)}</span>
      </summary>
      <div className="otel-span-body">
        <dl>
          <dt>trace_id</dt><dd className="mono">{span.traceId}</dd>
          <dt>span_id</dt><dd className="mono">{span.spanId}</dd>
          <dt>parent_span_id</dt><dd className="mono">{span.parentSpanId || '—'}</dd>
          <dt>status</dt><dd>{span.statusCode}</dd>
        </dl>
        <InvocationTrace open={open} invocation={span} subscription={subscription} resourceGroup={resourceGroup} app={app} timeRange={timeRange} startTime={startTime} endTime={endTime} />
      </div>
    </details>
  )
}

function localDateTimeValue(date: Date): string {
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}

function MetricsCard({ spans }: { spans: InvocationSpan[] }) {
  if (!spans.length) return null
  const durations = spans.map((span) => span.durationMs).sort((left, right) => left - right)
  const average = durations.reduce((sum, duration) => sum + duration, 0) / durations.length
  const p95 = durations[Math.max(0, Math.ceil(durations.length * 0.95) - 1)]
  const slowest = durations[durations.length - 1]
  const errors = spans.filter((span) => span.statusCode === 'STATUS_CODE_ERROR').length
  const errorRate = (errors / spans.length) * 100
  const maxDuration = Math.max(1, slowest)

  return (
    <details className="otel-metrics" open>
      <summary><span><strong>Metrics</strong><small>Current page only</small></span><span>Latency and health</span></summary>
      <div className="otel-metrics-body">
        <div className="otel-metric-grid">
          <div><span>Average latency</span><strong>{formatDuration(average)}</strong></div>
          <div><span>P95 latency</span><strong>{formatDuration(p95)}</strong></div>
          <div><span>Error rate</span><strong>{errorRate.toFixed(1)}%</strong></div>
          <div><span>Slowest</span><strong>{formatDuration(slowest)}</strong></div>
        </div>
        <div className="otel-latency-chart" role="img" aria-label="Invocation latency for the spans on this page">
          <div className="otel-chart-head"><strong>Latency by invocation</strong><span>{formatDuration(maxDuration)} max</span></div>
          <div className="otel-chart-bars">
            {spans.map((span, index) => (
              <span
                className={span.statusCode === 'STATUS_CODE_ERROR' ? 'error' : ''}
                key={`${span.traceId}:${span.spanId}:${index}`}
                style={{ height: `${Math.max(4, (span.durationMs / maxDuration) * 100)}%` }}
                title={`${new Date(span.startTime).toLocaleString()}: ${formatDuration(span.durationMs)}`}
              />
            ))}
          </div>
        </div>
      </div>
    </details>
  )
}

export function ObservabilityPanel({
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
  const [rangeMode, setRangeMode] = useState<RangeMode>('24h')
  const [customStart, setCustomStart] = useState(() => localDateTimeValue(new Date(Date.now() - 24 * 60 * 60_000)))
  const [customEnd, setCustomEnd] = useState(() => localDateTimeValue(new Date()))
  const [customRange, setCustomRange] = useState<{ startTime: string; endTime: string } | null>(null)
  const [customError, setCustomError] = useState('')
  const [page, setPage] = useState(1)
  const pageSize = 25
  const timeRange: TimeRange = rangeMode === 'custom' ? '24h' : rangeMode
  const startTime = rangeMode === 'custom' ? customRange?.startTime : undefined
  const endTime = rangeMode === 'custom' ? customRange?.endTime : undefined
  const rangeReady = rangeMode !== 'custom' || !!customRange
  const query = useQuery({
    queryKey: ['ai:invocations', subscription, resourceGroup, app, agentName, rangeMode, startTime, endTime, page, pageSize],
    queryFn: () => api.appInsightsQuery({
      subscription,
      resourceGroup,
      app,
      agent: agentName,
      preset: 'invocations',
      timeRange,
      startTime,
      endTime,
      page,
      pageSize,
    }),
    enabled: rangeReady,
    staleTime: 60_000,
    retry: false,
  })
  const invocationPage = rowsOf(query.data)
  const spans = invocationPage.spans
  const responseError = query.data?.error

  const applyCustomRange = () => {
    const startMs = Date.parse(customStart)
    const endMs = Date.parse(customEnd)
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs >= endMs) {
      setCustomError('Choose a start time before the end time.')
      return
    }
    setCustomError('')
    setCustomRange({ startTime: new Date(startMs).toISOString(), endTime: new Date(endMs).toISOString() })
    setPage(1)
  }

  useEffect(() => setPage(1), [subscription, resourceGroup, app, agentName])
  useEffect(() => {
    if (!query.isFetching && query.data && !responseError && page > 1 && spans.length === 0) setPage(1)
  }, [page, query.data, query.isFetching, responseError, spans.length])

  return (
    <section className="otel-panel" aria-busy={query.isFetching}>
      <div className="skill-section-head otel-panel-head">
        <div>
          <h2>Observability</h2>
          <p>One OpenTelemetry span per invocation, using runtime telemetry when available and Azure Functions host telemetry as a fallback.</p>
        </div>
        <div className="otel-controls">
          <div className="copy-as-tabs" role="tablist" aria-label="Invocation time range">
            {(['1h', '24h', 'custom'] as const).map((range) => (
              <button key={range} type="button" role="tab" aria-selected={rangeMode === range} className={'copy-as-tab' + (rangeMode === range ? ' is-active' : '')} onClick={() => { setRangeMode(range); setPage(1) }}>
                {range === 'custom' ? 'Custom' : range}
              </button>
            ))}
          </div>
          <Button size="small" disabled={query.isFetching} onClick={() => void query.refetch()}>
            {query.isFetching ? 'Refreshing…' : 'Refresh'}
          </Button>
        </div>
      </div>

      {rangeMode === 'custom' && (
        <div className="otel-custom-range">
          <label>Start<input type="datetime-local" value={customStart} max={customEnd} onChange={(event) => setCustomStart(event.target.value)} /></label>
          <label>End<input type="datetime-local" value={customEnd} min={customStart} onChange={(event) => setCustomEnd(event.target.value)} /></label>
          <Button size="small" onClick={applyCustomRange}>Apply</Button>
          {customError && <span className="otel-custom-error" role="alert">{customError}</span>}
        </div>
      )}

      {rangeReady && query.isLoading && <div className="skeleton shimmer-bar" />}
      {rangeReady && query.error && <div className="note warn" role="alert"><strong>Couldn’t load invocation spans.</strong><br />{(query.error as Error).message}</div>}
      {rangeReady && !query.isLoading && !query.error && responseError && <div className="note warn" role="alert"><strong>Application Insights returned an error.</strong><br />{responseError}</div>}
      {rangeReady && !query.isLoading && !query.error && !responseError && (
        <>
          <MetricsCard spans={spans} />
          {spans.length === 0 ? (
            <div className="empty otel-empty">
              <strong>No invocation spans found</strong>
              <span>No runtime or Azure Functions host invocations were recorded in this time range.</span>
              <span>Confirm the app has <span className="mono">APPLICATIONINSIGHTS_CONNECTION_STRING</span> configured. Install <span className="mono">azurefunctions-agents-runtime[monitor]</span> for richer agent attributes.</span>
            </div>
          ) : (
            <div className="otel-span-list">
              {spans.map((span, index) => (
                <InvocationRow key={`${span.traceId}:${span.spanId}:${index}`} span={span} subscription={subscription} resourceGroup={resourceGroup} app={app} timeRange={timeRange} startTime={startTime} endTime={endTime} />
              ))}
            </div>
          )}
          {(page > 1 || invocationPage.hasMore) && (
            <nav className="otel-pagination" aria-label="Invocation pages">
              <Button size="small" disabled={page === 1 || query.isFetching} onClick={() => setPage((current) => Math.max(1, current - 1))}>Previous</Button>
              <span>Page <strong>{page}</strong></span>
              <Button size="small" disabled={!invocationPage.hasMore || query.isFetching} onClick={() => setPage((current) => current + 1)}>Next</Button>
            </nav>
          )}
        </>
      )}
    </section>
  )
}