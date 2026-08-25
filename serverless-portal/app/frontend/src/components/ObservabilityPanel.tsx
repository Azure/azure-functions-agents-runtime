import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Button } from '@coreai/fluentui-react'
import { api, type AppInsightsResult } from '../api'

type TimeRange = '1h' | '24h' | '7d'

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

function rowsOf(result?: AppInsightsResult): InvocationSpan[] {
  const table = result?.tables?.[0]
  if (!table) return []
  const index = new Map(table.columns.map((column, position) => [column.name, position]))
  const value = (row: unknown[], name: string) => row[index.get(name) ?? -1]
  return table.rows.map((row) => ({
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
  const [timeRange, setTimeRange] = useState<TimeRange>('24h')
  const query = useQuery({
    queryKey: ['ai:invocations', subscription, resourceGroup, app, agentName, timeRange],
    queryFn: () => api.appInsightsQuery({
      subscription,
      resourceGroup,
      app,
      agent: agentName,
      preset: 'invocations',
      timeRange,
    }),
    staleTime: 60_000,
    retry: false,
  })
  const spans = rowsOf(query.data)
  const failures = spans.filter((span) => span.statusCode === 'STATUS_CODE_ERROR').length

  return (
    <section className="otel-panel" aria-busy={query.isFetching}>
      <div className="skill-section-head otel-panel-head">
        <div>
          <h2>Observability</h2>
          <p>One OpenTelemetry span per invocation, loaded from Application Insights only while this tab is open.</p>
        </div>
        <div className="otel-controls">
          <div className="copy-as-tabs" role="tablist" aria-label="Invocation time range">
            {(['1h', '24h', '7d'] as const).map((range) => (
              <button key={range} type="button" role="tab" aria-selected={timeRange === range} className={'copy-as-tab' + (timeRange === range ? ' is-active' : '')} onClick={() => setTimeRange(range)}>
                {range}
              </button>
            ))}
          </div>
          <Button size="small" disabled={query.isFetching} onClick={() => void query.refetch()}>
            {query.isFetching ? 'Refreshing…' : 'Refresh'}
          </Button>
        </div>
      </div>

      {query.isLoading && <div className="skeleton shimmer-bar" />}
      {query.error && <div className="note warn">Couldn’t load invocation spans: {(query.error as Error).message}</div>}
      {!query.isLoading && !query.error && (
        <>
          <div className="otel-summary">
            <span><strong>{spans.length}</strong> invocations</span>
            <span><strong>{failures}</strong> errors</span>
            <span>Last {timeRange}</span>
          </div>
          {spans.length === 0 ? (
            <div className="empty otel-empty">
              <strong>No runtime invocation spans found</strong>
              <span>No <span className="mono">agent.run</span> spans were exported in this time range.</span>
              <span>Confirm the app installs <span className="mono">azurefunctions-agents-runtime[monitor]</span> and has <span className="mono">APPLICATIONINSIGHTS_CONNECTION_STRING</span> configured.</span>
            </div>
          ) : (
            <div className="otel-span-list">
              {spans.map((span, index) => (
                <details className="otel-span" key={`${span.traceId}:${span.spanId}:${index}`}>
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
                    <pre>{JSON.stringify(asOtlpSpan(span), null, 2)}</pre>
                  </div>
                </details>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  )
}