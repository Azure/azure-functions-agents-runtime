import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { useToast } from '../toast'

const NAME_RE = /^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$/

export default function CreateAgentPage() {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [instructions, setInstructions] = useState('')
  const [builtin, setBuiltin] = useState(true)
  const [busy, setBusy] = useState(false)
  const toast = useToast()
  const navigate = useNavigate()

  const fileName = `${name.trim() || '<name>'}.agent.md`

  const preview = useMemo(() => {
    const n = name.trim() || '<name>'
    const instr = instructions.trim() || 'You are a helpful assistant. Answer concisely.'
    let fm = `name: ${n}\ndescription: ${description.trim()}`
    if (builtin) fm += `\nbuiltin_endpoints: true`
    return `---\n${fm}\n---\n\n${instr}`
  }, [name, description, instructions, builtin])

  async function create() {
    const n = name.trim().toLowerCase()
    if (!NAME_RE.test(n)) {
      toast('Name must be lowercase letters, digits, or hyphens (1-40 chars).', 'err')
      return
    }
    setBusy(true)
    try {
      await api.create({
        name: n,
        description: description.trim(),
        instructions,
        builtin_endpoints: builtin,
      })
      toast(`Created ${n}.agent.md`, 'ok')
      navigate(`/edit/${encodeURIComponent(n)}`)
    } catch (e) {
      toast((e as Error).message, 'err')
      setBusy(false)
    }
  }

  return (
    <>
      <div className="breadcrumb">
        <Link to="/">Agents</Link> / Create
      </div>
      <div className="page-title">
        <h1>Create agent</h1>
      </div>
      <p className="page-sub">
        Generates a valid <span className="mono">&lt;name&gt;.agent.md</span> and stores it in blob.
      </p>

      <div className="grid cols-2">
        <div className="card">
          <h3>Basics</h3>
          <div className="field">
            <label>Name</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="support-triage"
              autoComplete="off"
            />
            <div className="hint">
              Lowercase letters, digits, hyphens. Becomes <span className="mono">{fileName}</span>.
            </div>
          </div>
          <div className="field">
            <label>Description</label>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Triages inbound support tickets."
            />
          </div>
          <div className="field">
            <label>Instructions</label>
            <textarea
              rows={6}
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
              placeholder="You are a support triage assistant…"
            />
            <div className="hint">Markdown body of the agent — its system instructions.</div>
          </div>
          <div className="field">
            <label>
              <input
                type="checkbox"
                checked={builtin}
                onChange={(e) => setBuiltin(e.target.checked)}
                style={{ width: 'auto', marginRight: 8 }}
              />
              Expose built-in endpoints
            </label>
            <div className="hint">
              Adds chat UI, chat API, SSE stream, and an MCP tool (
              <span className="mono">builtin_endpoints: true</span>).
            </div>
          </div>
          <div className="pill-row">
            <button className="btn primary" onClick={create} disabled={busy}>
              Create &amp; edit
            </button>
            <Link className="btn" to="/">
              Cancel
            </Link>
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <h3>Preview</h3>
            <span className="badge blue">{fileName}</span>
          </div>
          <pre className="code" style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
            {preview}
          </pre>
          <p className="hint">
            This is exactly what gets written to storage. Refine it in the editor after creating.
          </p>
        </div>
      </div>
    </>
  )
}
