import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { composerApi } from '../api'
import { NODE_META, type WorkflowSummary } from '../workflow/types'

const EXAMPLES = [
  'When a support email arrives, classify it, look up the customer, draft a reply grounded in our support taxonomy, and open a ticket.',
  'Every weekday at 8am, pull yesterday’s Azure cost, flag anything that spiked over 20%, and post a digest to Teams.',
  'When a receipt image is uploaded, extract the line items, categorize them, and save the result.',
  'On a new GitHub issue, triage severity, summarize it, and open a matching Azure DevOps work item.',
]

function ComponentPills({ counts }: { counts: Record<string, number> }) {
  const order = ['trigger', 'agent', 'tool', 'skill', 'mcp', 'output'] as const
  return (
    <div className="pill-row">
      {order
        .filter((t) => counts[t])
        .map((t) => (
          <span key={t} className={`tag ${NODE_META[t].cls}`}>
            {counts[t]} {NODE_META[t].label.toLowerCase()}
            {counts[t] > 1 ? 's' : ''}
          </span>
        ))}
    </div>
  )
}

export default function WorkflowsPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [prompt, setPrompt] = useState('')

  const { data: workflows, isLoading } = useQuery({
    queryKey: ['workflows'],
    queryFn: () => composerApi.list(),
  })
  const { data: model } = useQuery({ queryKey: ['composer-model'], queryFn: () => composerApi.model() })

  const generate = useMutation({
    mutationFn: (p: string) => composerApi.generate(p),
    onSuccess: (wf) => {
      qc.invalidateQueries({ queryKey: ['workflows'] })
      navigate(`/workflows/${wf.id}`)
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => composerApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['workflows'] }),
  })

  const list = workflows ?? []

  return (
    <>
      <div className="breadcrumb">Home / Workflows</div>
      <div className="page-title">
        <h1>Workflows</h1>
      </div>
      <p className="page-sub">
        Describe an app in plain language — the composer generates the triggers, agents, tools &amp;
        skills, wires them together on Azure Functions, and gives you a shareable run surface.
      </p>

      {/* Prompt hero */}
      <div className="hero-prompt">
        <h3>✨ Describe your app</h3>
        <textarea
          rows={3}
          placeholder="e.g. When a support email arrives, classify it, look up the customer, draft a reply, and open a ticket."
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
        />
        <div className="hero-actions">
          <button
            className="btn primary"
            disabled={!prompt.trim() || generate.isPending}
            onClick={() => generate.mutate(prompt.trim())}
          >
            {generate.isPending ? '✨ Generating…' : '✨ Generate workflow'}
          </button>
          <span className="model-chip" title="The model used to generate — separate from the skills that shape the prompt.">
            model: <strong>{model?.model ?? '…'}</strong> · skill: <strong>composer-plan</strong>
          </span>
        </div>
        {generate.isError && (
          <div className="note" style={{ marginTop: 10 }}>
            {(generate.error as Error).message}
          </div>
        )}
        <div className="example-row">
          {EXAMPLES.map((ex) => (
            <button key={ex} className="example-chip" onClick={() => setPrompt(ex)} title={ex}>
              {ex.length > 54 ? ex.slice(0, 54) + '…' : ex}
            </button>
          ))}
        </div>
      </div>

      <div className="divider" />

      <div className="toolbar" style={{ justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0, fontSize: 15 }}>Your workflows</h3>
      </div>

      {isLoading && <div className="empty">Loading…</div>}
      {!isLoading && list.length === 0 && (
        <div className="empty">No workflows yet — describe one above to get started.</div>
      )}

      <div className="grid cols-3">
        {list.map((wf: WorkflowSummary) => (
          <div className="card wf-card" key={wf.id}>
            <div className="card-head">
              <h3>
                <Link to={`/workflows/${wf.id}`}>{wf.name}</Link>
              </h3>
              <span className={`badge ${wf.status === 'published' ? 'green' : 'amber'}`}>
                <span className="dot" />
                {wf.status}
              </span>
            </div>
            <p className="muted" style={{ fontSize: 12.5, margin: '0 0 12px', minHeight: 34 }}>
              {wf.description}
            </p>
            <ComponentPills counts={wf.componentCounts} />
            <div className="wf-foot">
              <span className="muted">
                v{wf.version} · {wf.target?.functionApp ?? 'no target'}
              </span>
              <span className="wf-actions">
                <Link to={`/workflows/${wf.id}`} className="btn sm">Open</Link>
                <Link to={`/workflows/${wf.id}/run`} className="btn sm">Run</Link>
                <button
                  className="btn sm ghost"
                  title="Delete workflow"
                  onClick={() => {
                    if (confirm(`Delete "${wf.name}"?`)) remove.mutate(wf.id)
                  }}
                >
                  🗑
                </button>
              </span>
            </div>
          </div>
        ))}
      </div>
    </>
  )
}
