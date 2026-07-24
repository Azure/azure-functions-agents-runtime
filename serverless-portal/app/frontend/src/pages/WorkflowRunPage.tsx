import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { composerApi } from '../api'
import { NODE_META, type RunResult } from '../workflow/types'

export default function WorkflowRunPage() {
  const { id } = useParams<{ id: string }>()
  const [values, setValues] = useState<Record<string, string>>({})
  const [result, setResult] = useState<RunResult | null>(null)

  const { data: workflow, isLoading } = useQuery({
    queryKey: ['workflow', id],
    queryFn: () => composerApi.get(id!),
    enabled: !!id,
  })

  const run = useMutation({
    mutationFn: (inputs: Record<string, unknown>) => composerApi.run(id!, inputs),
    onSuccess: (r) => setResult(r),
  })

  const inputs = workflow?.inputs ?? []
  const running = run.isPending
  const steps = useMemo(() => result?.steps ?? [], [result])

  if (isLoading || !workflow) return <div className="empty">Loading…</div>

  return (
    <>
      <div className="breadcrumb">
        Home / <Link to="/workflows">Workflows</Link> / <Link to={`/workflows/${id}`}>{workflow.name}</Link> / Run
      </div>
      <div className="page-title">
        <h1>{workflow.name}</h1>
        <span className={`badge ${workflow.status === 'published' ? 'green' : 'amber'}`}>
          <span className="dot" />
          {workflow.status}
        </span>
      </div>
      <p className="page-sub">{workflow.description}</p>

      <div className="run-wrap">
        {/* Input form generated from workflow.inputs */}
        <div className="card run-form">
          <h3>Test input</h3>
          {inputs.length === 0 && <p className="muted" style={{ fontSize: 12.5 }}>This workflow has no input fields.</p>}
          {inputs.map((f) => (
            <div className="field" key={f.id}>
              <label>
                {f.label} {f.required && <span style={{ color: 'var(--red)' }}>*</span>}
              </label>
              {f.type === 'textarea' ? (
                <textarea
                  rows={5}
                  placeholder={f.placeholder}
                  value={values[f.id] ?? ''}
                  onChange={(e) => setValues((v) => ({ ...v, [f.id]: e.target.value }))}
                />
              ) : (
                <input
                  type="text"
                  placeholder={f.placeholder}
                  value={values[f.id] ?? ''}
                  onChange={(e) => setValues((v) => ({ ...v, [f.id]: e.target.value }))}
                />
              )}
            </div>
          ))}
          <button
            className="btn primary"
            style={{ width: '100%', justifyContent: 'center' }}
            disabled={running}
            onClick={() => run.mutate(values)}
          >
            {running ? 'Running…' : '▶ Run workflow'}
          </button>
          <div className="divider" />
          <dl className="kv">
            <dt>Target app</dt>
            <dd className="mono">{workflow.target?.functionApp ?? '—'}</dd>
            <dt>Share link</dt>
            <dd className="mono">/run/{workflow.id}</dd>
          </dl>
        </div>

        {/* Live trace */}
        <div className="card">
          <div className="card-head">
            <h3>Run trace</h3>
            {result && (
              <span className="badge green">
                <span className="dot" />
                {result.status} · {(result.durationMs / 1000).toFixed(1)}s
              </span>
            )}
          </div>
          {!result && !running && <div className="empty">Provide input and run to see the per-step trace.</div>}
          {running && <div className="empty">Running…</div>}
          {steps.length > 0 && (
            <ul className="timeline">
              {steps.map((s) => (
                <li className="done" key={s.nodeId}>
                  <span className="dot">✓</span>
                  <div className="step-title">
                    <span className={`tag ${NODE_META[s.type]?.cls ?? 'gray'}`}>
                      {NODE_META[s.type]?.icon} {s.type}
                    </span>
                    {s.name}
                    <span className="muted" style={{ marginLeft: 'auto', fontSize: 11 }}>
                      {s.elapsedMs}ms
                    </span>
                  </div>
                  <div className="step-out code" style={{ whiteSpace: 'pre-wrap' }}>{s.output}</div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </>
  )
}
