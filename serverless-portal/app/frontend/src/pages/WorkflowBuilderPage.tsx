import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { composerApi, ApiError } from '../api'
import Canvas from '../workflow/Canvas'
import { NodeEditor } from '../workflow/editors'
import { Field, TextInput, TextArea, Select } from '../workflow/fields'
import {
  NODE_META,
  type NodeType,
  type Workflow,
  type WorkflowNode,
  type WorkflowInput,
  type WorkflowVersion,
} from '../workflow/types'

const PALETTE: NodeType[] = ['trigger', 'agent', 'tool', 'skill', 'mcp', 'output', 'router']

function defaultNode(type: NodeType): Omit<WorkflowNode, 'id' | 'position'> {
  switch (type) {
    case 'trigger':
      return { type, kind: 'httpTrigger', name: 'HTTP request', source: 'manual', config: { route: 'run', methods: ['POST'] } }
    case 'agent':
      return { type, kind: 'agent', name: 'New Agent', source: 'manual', config: { sourceFile: 'new.agent.md', instructions: '', skills: [], tools: [], builtinEndpoints: false } }
    case 'tool':
      return { type, kind: 'tool', name: 'new_tool', source: 'manual', config: { signature: 'def new_tool(x: str) -> str', code: '@tool\ndef new_tool(x: str) -> str:\n    """TODO."""\n    ...' } }
    case 'skill':
      return { type, kind: 'skill', name: 'new-skill', source: 'manual', config: { path: 'skills/new-skill/SKILL.md', summary: '' } }
    case 'mcp':
      return { type, kind: 'mcp', name: 'mcp-server', source: 'manual', config: { server: 'server', url: '', tools: [] } }
    case 'output':
      return { type, kind: 'connector', name: 'Output', source: 'manual', config: { connector: '', action: '' } }
    case 'router':
      return { type, kind: 'switch', name: 'Router', source: 'manual', config: { on: '', cases: [] } }
  }
}

let nodeSeq = 0
const genId = (t: string) => `n_${t}_${Date.now().toString(36)}_${nodeSeq++}`
const genEdge = () => `e_${Date.now().toString(36)}_${nodeSeq++}`

// Give the graph a clean left-to-right layered layout in pixel coordinates.
// Applied on every load so a graph from the generator (grid coords), the model
// (arbitrary/degenerate coords), or a restore never renders stacked. A layout the
// user has already spread out by dragging (wide spread, no overlaps) is kept.
function layoutWorkflow(wf: Workflow): Workflow {
  const nodes = wf.nodes
  if (nodes.length === 0) return wf

  const xs = nodes.map((n) => n.position?.x ?? 0)
  const spread = Math.max(...xs) - Math.min(...xs)
  const distinct = new Set(
    nodes.map((n) => `${Math.round(n.position?.x ?? 0)},${Math.round(n.position?.y ?? 0)}`),
  ).size
  // Already a real, non-overlapping pixel layout (e.g. user-dragged) → leave it.
  if (spread > 220 && distinct === nodes.length) return wf

  // Longest-path depth = column; order within a column = row.
  const idset = new Set(nodes.map((n) => n.id))
  const incoming = new Map<string, string[]>(nodes.map((n) => [n.id, []]))
  for (const e of wf.edges) {
    if (idset.has(e.from) && idset.has(e.to)) incoming.get(e.to)!.push(e.from)
  }
  const col = new Map<string, number>()
  const visiting = new Set<string>()
  const depth = (id: string): number => {
    if (col.has(id)) return col.get(id)!
    if (visiting.has(id)) return 0 // cycle guard
    visiting.add(id)
    const ins = incoming.get(id) ?? []
    const d = ins.length === 0 ? 0 : Math.max(...ins.map(depth)) + 1
    visiting.delete(id)
    col.set(id, d)
    return d
  }
  nodes.forEach((n) => depth(n.id))

  const byCol = new Map<number, WorkflowNode[]>()
  nodes.forEach((n) => {
    const c = col.get(n.id) ?? 0
    const arr = byCol.get(c) ?? []
    arr.push(n)
    byCol.set(c, arr)
  })

  const COLW = 260
  const ROWH = 150
  const PADX = 48
  const PADY = 40
  const pos = new Map<string, { x: number; y: number }>()
  ;[...byCol.keys()]
    .sort((a, b) => a - b)
    .forEach((c) => {
      byCol.get(c)!.forEach((n, r) => pos.set(n.id, { x: PADX + c * COLW, y: PADY + r * ROWH }))
    })
  return { ...wf, nodes: nodes.map((n) => ({ ...n, position: pos.get(n.id) ?? { x: PADX, y: PADY } })) }
}

export default function WorkflowBuilderPage() {
  const { id } = useParams<{ id: string }>()
  const qc = useQueryClient()
  const [draft, setDraft] = useState<Workflow | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [showFiles, setShowFiles] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [conflict, setConflict] = useState(false)
  const [regenText, setRegenText] = useState('')
  const savedRef = useRef<string>('')

  const { data, isLoading, error } = useQuery({
    queryKey: ['workflow', id],
    queryFn: () => composerApi.get(id!),
    enabled: !!id,
  })
  const { data: model } = useQuery({ queryKey: ['composer-model'], queryFn: () => composerApi.model() })

  // Load a workflow into the editable draft with a clean pixel layout, and set
  // the saved baseline so it isn't falsely marked dirty.
  const hydrateFrom = useCallback((wf: Workflow) => {
    const laid = layoutWorkflow(wf)
    setDraft(structuredClone(laid))
    savedRef.current = JSON.stringify(laid)
  }, [])

  // Hydrate the editable draft when the workflow loads or changes identity.
  useEffect(() => {
    if (data && (!draft || draft.id !== data.id)) {
      hydrateFrom(data)
      setRegenText(data.prompt ?? '')
    }
  }, [data, draft, hydrateFrom])

  const dirty = draft ? JSON.stringify(draft) !== savedRef.current : false

  const save = useMutation({
    mutationFn: () => composerApi.save(id!, draft!),
    onSuccess: (wf) => {
      setConflict(false)
      hydrateFrom(wf)
      qc.setQueryData(['workflow', id], wf)
      qc.invalidateQueries({ queryKey: ['workflows'] })
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) setConflict(true)
    },
  })
  // Overwrite the server copy, ignoring the concurrency check (resolve a conflict).
  const forceSave = useMutation({
    mutationFn: () => composerApi.save(id!, { ...draft!, etag: undefined }),
    onSuccess: (wf) => {
      setConflict(false)
      hydrateFrom(wf)
      qc.setQueryData(['workflow', id], wf)
      qc.invalidateQueries({ queryKey: ['workflows'] })
    },
  })
  const restore = useMutation({
    mutationFn: (version: number) => composerApi.restore(id!, version),
    onSuccess: (wf) => {
      hydrateFrom(wf)
      setSelectedId(null)
      setShowHistory(false)
      qc.setQueryData(['workflow', id], wf)
      qc.invalidateQueries({ queryKey: ['workflows'] })
    },
  })
  const deploy = useMutation({
    mutationFn: () => composerApi.deploy(id!),
    onSuccess: (wf) => {
      hydrateFrom(wf)
      qc.setQueryData(['workflow', id], wf)
      qc.invalidateQueries({ queryKey: ['workflows'] })
    },
  })
  const regenerate = useMutation({
    mutationFn: (p: string) => composerApi.regenerate(id!, p),
    onSuccess: (wf) => {
      hydrateFrom(wf)
      setSelectedId(null)
      qc.setQueryData(['workflow', id], wf)
    },
  })
  const files = useQuery({
    queryKey: ['workflow-compile', id, draft?.version],
    queryFn: () => composerApi.compile(id!),
    enabled: !!id && showFiles,
  })

  const selectedNode = useMemo(
    () => draft?.nodes.find((n) => n.id === selectedId) ?? null,
    [draft, selectedId],
  )

  if (isLoading || !draft) return <div className="empty">Loading workflow…</div>
  if (error) return <div className="empty">Failed to load: {(error as Error).message}</div>

  // ---- graph mutations (local) ----
  const patchNode = (nodeId: string, patch: Partial<WorkflowNode>) =>
    setDraft((d) =>
      d ? { ...d, nodes: d.nodes.map((n) => (n.id === nodeId ? { ...n, ...patch, config: patch.config ?? n.config } : n)) } : d,
    )
  const deleteNode = (nodeId: string) =>
    setDraft((d) =>
      d
        ? { ...d, nodes: d.nodes.filter((n) => n.id !== nodeId), edges: d.edges.filter((e) => e.from !== nodeId && e.to !== nodeId) }
        : d,
    )
  const addNode = (type: NodeType) => {
    const maxX = draft.nodes.reduce((m, n) => Math.max(m, n.position?.x ?? 0), 0)
    const node: WorkflowNode = { ...defaultNode(type), id: genId(type), position: { x: maxX + 260, y: 60 } }
    setDraft((d) => (d ? { ...d, nodes: [...d.nodes, node] } : d))
    setSelectedId(node.id)
  }
  const moveNode = (nodeId: string, x: number, y: number) =>
    setDraft((d) =>
      d ? { ...d, nodes: d.nodes.map((n) => (n.id === nodeId ? { ...n, position: { x, y } } : n)) } : d,
    )
  const patchMeta = (patch: Partial<Workflow>) => setDraft((d) => (d ? { ...d, ...patch } : d))
  const addEdge = (from: string, to: string, label: string) =>
    setDraft((d) => (d ? { ...d, edges: [...d.edges, { id: genEdge(), from, to, label }] } : d))
  const removeEdge = (edgeId: string) =>
    setDraft((d) => (d ? { ...d, edges: d.edges.filter((e) => e.id !== edgeId) } : d))
  // Discard local edits and reload the latest server copy (resolve a conflict).
  const reload = async () => {
    const fresh = await composerApi.get(id!)
    hydrateFrom(fresh)
    setConflict(false)
    qc.setQueryData(['workflow', id], fresh)
  }

  const statusLabel =
    draft.status === 'published_with_changes' ? 'changes pending' : draft.status
  const statusColor =
    draft.status === 'published' ? 'green' : draft.status === 'published_with_changes' ? 'blue' : 'amber'

  return (
    <div className="builder">
      {/* Toolbar */}
      <div className="builder-bar">
        <Link to="/workflows" className="btn ghost sm">← Workflows</Link>
        <input
          className="wf-name-input"
          value={draft.name}
          onChange={(e) => patchMeta({ name: e.target.value })}
        />
        <span className={`badge ${statusColor}`}>
          <span className="dot" />
          {statusLabel}
        </span>
        <span className="muted" style={{ fontSize: 12 }}>v{draft.version}</span>
        <div style={{ flex: 1 }} />
        {dirty && <span className="muted" style={{ fontSize: 12 }}>● unsaved</span>}
        <button className="btn sm" onClick={() => setShowHistory(true)}>🕐 History</button>
        <button className="btn sm" onClick={() => setShowFiles(true)}>📄 Project files</button>
        <Link className="btn sm" to={`/workflows/${id}/run`}>▶ Test run</Link>
        <button className="btn sm primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? 'Saving…' : '💾 Save'}
        </button>
        <button className="btn sm" style={{ background: 'var(--green)', color: '#fff', borderColor: 'var(--green)' }} disabled={deploy.isPending} onClick={() => deploy.mutate()}>
          {deploy.isPending ? 'Deploying…' : draft.publishedVersion != null ? '🚀 Re-Deploy' : '🚀 Deploy'}
        </button>
      </div>

      {conflict && (
        <div className="conflict-banner">
          <span>⚠ This workflow was changed elsewhere since you opened it — your save was blocked.</span>
          <span style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
            <button className="btn sm" onClick={reload}>Reload latest</button>
            <button className="btn sm danger" disabled={forceSave.isPending} onClick={() => forceSave.mutate()}>
              {forceSave.isPending ? 'Overwriting…' : 'Overwrite anyway'}
            </button>
          </span>
        </div>
      )}

      <div className="builder-body">
        {/* LEFT: prompt + palette + component list */}
        <aside className="builder-left">
          <h2>Describe</h2>
          <TextArea value={regenText} onChange={setRegenText} rows={4} placeholder="Describe the app…" />
          <button
            className="btn primary sm"
            style={{ marginTop: 8 }}
            disabled={regenerate.isPending || !regenText.trim()}
            onClick={() => regenerate.mutate(regenText.trim())}
          >
            {regenerate.isPending ? '✨ Regenerating…' : '✨ Regenerate'}
          </button>
          {regenerate.isError && <div className="note" style={{ marginTop: 8 }}>{(regenerate.error as Error).message}</div>}
          <div className="model-chip" style={{ marginTop: 10 }}>
            model: <strong>{model?.model ?? '…'}</strong> · skill: <strong>{draft.generation?.skill ?? 'composer-plan'}</strong>
          </div>

          <h2 style={{ marginTop: 22 }}>Add component</h2>
          <div className="palette">
            {PALETTE.map((t) => (
              <button key={t} className="chip" onClick={() => addNode(t)}>
                <span className={`tag ${NODE_META[t].cls}`}>{NODE_META[t].icon}</span> {NODE_META[t].label}
              </button>
            ))}
          </div>

          <h2 style={{ marginTop: 22 }}>Components ({draft.nodes.length})</h2>
          <div className="stack">
            {draft.nodes.map((n) => (
              <button
                key={n.id}
                className={`comp-row${selectedId === n.id ? ' active' : ''}`}
                onClick={() => setSelectedId(n.id)}
              >
                <span className={`tag ${NODE_META[n.type].cls}`}>{NODE_META[n.type].icon}</span>
                <span className="comp-name">{n.name}</span>
                <span className="muted" style={{ fontSize: 11 }}>{n.kind}</span>
              </button>
            ))}
          </div>
        </aside>

        {/* CENTER: canvas */}
        <section className="builder-canvas">
          <Canvas
            workflow={draft}
            selectedId={selectedId}
            dirty={dirty}
            onSelect={setSelectedId}
            onMoveNode={moveNode}
            onConnect={(from, to) => {
              if (!draft.edges.some((e) => e.from === from && e.to === to)) addEdge(from, to, '')
            }}
            onDeleteEdge={removeEdge}
            onSave={() => save.mutate()}
            regenerating={regenerate.isPending}
            onRegenerate={() => {
              if (confirm('Regenerate the workflow from its description? This replaces the current graph.')) {
                regenerate.mutate((regenText || draft.prompt || '').trim())
              }
            }}
          />
        </section>

        {/* RIGHT: inspector — component editor or workflow settings */}
        <aside className="builder-right">
          {selectedNode ? (
            <>
              <div className="row-between" style={{ marginBottom: 10 }}>
                <h2 style={{ margin: 0 }}>Edit component</h2>
                <button className="btn ghost sm" onClick={() => setSelectedId(null)}>Settings ⚙</button>
              </div>
              <NodeEditor
                node={selectedNode}
                onChange={(patch) => patchNode(selectedNode.id, patch)}
                onGenerate={async (node) => {
                  const context = {
                    name: draft.name,
                    prompt: draft.prompt,
                    nodes: draft.nodes,
                    edges: draft.edges,
                  }
                  return composerApi.generateCode(node, context)
                }}
                onDelete={() => {
                  deleteNode(selectedNode.id)
                  setSelectedId(null)
                }}
              />
            </>
          ) : (
            <WorkflowSettings
              draft={draft}
              patchMeta={patchMeta}
              addEdge={addEdge}
              removeEdge={removeEdge}
            />
          )}
        </aside>
      </div>

      {showFiles && (
        <ProjectFilesModal files={files.data?.files ?? {}} loading={files.isLoading} onClose={() => setShowFiles(false)} />
      )}
      {showHistory && (
        <HistoryModal
          versions={draft.history ?? []}
          currentVersion={draft.version}
          publishedVersion={draft.publishedVersion ?? null}
          restoring={restore.isPending}
          onRestore={(v) => {
            if (confirm(`Restore version ${v}? This creates a new version from that snapshot.`)) restore.mutate(v)
          }}
          onClose={() => setShowHistory(false)}
        />
      )}
    </div>
  )
}

// ---------- Workflow-level settings (shown when no node is selected) ----------
function WorkflowSettings({
  draft,
  patchMeta,
  addEdge,
  removeEdge,
}: {
  draft: Workflow
  patchMeta: (p: Partial<Workflow>) => void
  addEdge: (from: string, to: string, label: string) => void
  removeEdge: (id: string) => void
}) {
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [label, setLabel] = useState('')
  const nodeName = (nid: string) => draft.nodes.find((n) => n.id === nid)?.name ?? nid
  const opts = draft.nodes.map((n) => ({ value: n.id, label: `${NODE_META[n.type].icon} ${n.name}` }))

  const setInput = (i: number, patch: Partial<WorkflowInput>) =>
    patchMeta({ inputs: draft.inputs.map((f, j) => (j === i ? { ...f, ...patch } : f)) })
  const addInput = () =>
    patchMeta({ inputs: [...draft.inputs, { id: `input_${draft.inputs.length}`, label: 'New input', type: 'text' }] })
  const removeInput = (i: number) => patchMeta({ inputs: draft.inputs.filter((_, j) => j !== i) })
  const setTarget = (patch: Partial<Workflow['target']>) => patchMeta({ target: { ...draft.target, ...patch } })

  return (
    <div>
      <h2>Workflow settings</h2>
      <Field label="Description">
        <TextArea value={draft.description} onChange={(v) => patchMeta({ description: v })} rows={2} />
      </Field>

      <div className="divider" />
      <h2>Deployment target</h2>
      <Field label="Function App" hint="Provisioned & deployed on your behalf.">
        <TextInput value={draft.target.functionApp ?? ''} onChange={(v) => setTarget({ functionApp: v })} mono />
      </Field>
      <Field label="Resource group">
        <TextInput value={draft.target.resourceGroup ?? ''} onChange={(v) => setTarget({ resourceGroup: v })} mono />
      </Field>
      <Field label="Model provider">
        <TextInput value={draft.target.provider ?? ''} onChange={(v) => setTarget({ provider: v })} placeholder="foundry" />
      </Field>

      <div className="divider" />
      <h2>Run-surface inputs</h2>
      {draft.inputs.map((f, i) => (
        <div className="edge-row" key={f.id}>
          <input value={f.label} onChange={(e) => setInput(i, { label: e.target.value })} />
          <Select
            value={f.type}
            onChange={(v) => setInput(i, { type: v as WorkflowInput['type'] })}
            options={['text', 'textarea', 'number', 'choice', 'file', 'boolean'].map((t) => ({ value: t, label: t }))}
          />
          <button className="btn ghost sm" onClick={() => removeInput(i)}>×</button>
        </div>
      ))}
      <button className="btn sm" onClick={addInput}>＋ Add input</button>

      <div className="divider" />
      <h2>Connections (glue)</h2>
      <div className="stack">
        {draft.edges.map((e) => (
          <div className="edge-chip" key={e.id}>
            <span>{nodeName(e.from)} → {nodeName(e.to)}</span>
            {e.label && <span className="muted" style={{ fontSize: 11 }}>{e.label}</span>}
            <button className="btn ghost sm" onClick={() => removeEdge(e.id)}>×</button>
          </div>
        ))}
      </div>
      <div className="edge-row" style={{ marginTop: 8 }}>
        <Select value={from} onChange={setFrom} options={[{ value: '', label: 'from…' }, ...opts]} />
        <Select value={to} onChange={setTo} options={[{ value: '', label: 'to…' }, ...opts]} />
      </div>
      <div className="edge-row">
        <input placeholder="payload label" value={label} onChange={(e) => setLabel(e.target.value)} />
        <button
          className="btn sm"
          disabled={!from || !to || from === to}
          onClick={() => {
            addEdge(from, to, label)
            setFrom('')
            setTo('')
            setLabel('')
          }}
        >
          ＋ Link
        </button>
      </div>

      {draft.generation && (
        <>
          <div className="divider" />
          <div className="muted" style={{ fontSize: 12 }}>
            Generated by <strong>{draft.generation.model}</strong> ({draft.generation.provider}) via skill{' '}
            <strong>{draft.generation.skill}</strong>.
          </div>
        </>
      )}

      {draft.deployment && (
        <>
          <div className="divider" />
          <h2>Last deployment</h2>
          <div className="note" style={{ background: 'var(--green-soft)', borderColor: '#bfe3cd', color: 'var(--green)' }}>
            ✓ {draft.deployment.status} on behalf of customer → {draft.deployment.target.functionApp}
          </div>
          <div className="stack" style={{ marginTop: 10 }}>
            {draft.deployment.endpoints.map((ep) => (
              <div className="edge-chip" key={ep.url}>
                <span className="badge gray">{ep.method}</span>
                <code style={{ fontSize: 11, overflowWrap: 'anywhere' }}>{ep.url}</code>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

// ---------- Project files preview modal ----------
function ProjectFilesModal({
  files,
  loading,
  onClose,
}: {
  files: Record<string, string>
  loading: boolean
  onClose: () => void
}) {
  const names = Object.keys(files)
  const [active, setActive] = useState<string | null>(null)
  const current = active ?? names[0] ?? null
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="row-between" style={{ marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Project files — compiled from the graph</h3>
          <button className="btn ghost sm" onClick={onClose}>✕</button>
        </div>
        {loading && <div className="empty">Compiling…</div>}
        {!loading && (
          <div className="files-wrap">
            <div className="files-list">
              {names.map((n) => (
                <button key={n} className={`file-row${current === n ? ' active' : ''}`} onClick={() => setActive(n)}>
                  {n}
                </button>
              ))}
            </div>
            <pre className="code files-content">{current ? files[current] : 'No files.'}</pre>
          </div>
        )}
      </div>
    </div>
  )
}

// ---------- Version history + restore ----------
function HistoryModal({
  versions,
  currentVersion,
  publishedVersion,
  restoring,
  onRestore,
  onClose,
}: {
  versions: WorkflowVersion[]
  currentVersion: number
  publishedVersion: number | null
  restoring: boolean
  onRestore: (version: number) => void
  onClose: () => void
}) {
  const sorted = [...versions].sort((a, b) => b.version - a.version)
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" style={{ width: 'min(560px, 92vw)' }} onClick={(e) => e.stopPropagation()}>
        <div className="row-between" style={{ marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Version history</h3>
          <button className="btn ghost sm" onClick={onClose}>✕</button>
        </div>
        {sorted.length === 0 && <div className="empty">No history yet.</div>}
        <div className="stack" style={{ maxHeight: '60vh', overflow: 'auto' }}>
          {sorted.map((v) => (
            <div className="version-row" key={v.version}>
              <span className="version-num">v{v.version}</span>
              <span className="version-msg">
                {v.message}
                {v.version === publishedVersion && <span className="badge green" style={{ marginLeft: 8 }}>deployed</span>}
                {v.version === currentVersion && <span className="badge blue" style={{ marginLeft: 8 }}>current</span>}
              </span>
              <span className="muted" style={{ fontSize: 11 }}>{new Date(v.updatedAt).toLocaleString()}</span>
              <button
                className="btn sm"
                disabled={restoring || v.version === currentVersion}
                onClick={() => onRestore(v.version)}
              >
                Restore
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
