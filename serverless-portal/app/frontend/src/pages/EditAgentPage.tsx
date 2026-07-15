import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import { useToast } from '../toast'

export default function EditAgentPage() {
  const { name = '' } = useParams()
  const [content, setContent] = useState('')
  const [original, setOriginal] = useState('')
  const [status, setStatus] = useState('loading')
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  const load = useCallback(async () => {
    setStatus('loading')
    try {
      const data = await api.get(name)
      setContent(data.content)
      setOriginal(data.content)
      setStatus('loaded')
    } catch (e) {
      toast((e as Error).message, 'err')
      setStatus('error')
    }
  }, [name, toast])

  useEffect(() => {
    load()
  }, [load])

  const dirty = content !== original

  const frontLines = useMemo(() => {
    const m = content.match(/^\s*---\s*\n([\s\S]*?)\n---/)
    return m ? m[1].split('\n').filter((l) => l.trim()) : null
  }, [content])

  async function save() {
    setBusy(true)
    try {
      await api.update(name, content)
      setOriginal(content)
      toast('Saved to storage', 'ok')
    } catch (e) {
      toast((e as Error).message, 'err')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="breadcrumb">
        <Link to="/">Agents</Link> / {name}
      </div>
      <div className="page-title">
        <h1>{name}</h1>
        <span className="badge blue">http</span>
      </div>
      <p className="page-sub">
        <span className="mono">agents/{name}.agent.md</span>
      </p>

      <div className="toolbar">
        <button className="btn primary" onClick={save} disabled={!dirty || busy}>
          Save
        </button>
        <button className="btn" onClick={load}>
          Revert
        </button>
        <span className={`badge ${dirty ? 'amber' : 'gray'}`}>
          {dirty ? 'unsaved changes' : status}
        </span>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <div className="card-head">
            <h3>{name}.agent.md</h3>
          </div>
          <textarea
            className="editor"
            spellCheck={false}
            value={content}
            onChange={(e) => setContent(e.target.value)}
          />
          <p className="hint">Full raw agent file: YAML front matter + markdown instructions.</p>
        </div>

        <div className="card">
          <h3>Parsed front matter</h3>
          <div className="mono muted">
            {frontLines
              ? frontLines.map((l, i) => <div key={i}>{l}</div>)
              : '(no front matter found)'}
          </div>
          <div className="divider" />
          <h3>Validation</h3>
          <div className="pill-row">
            {frontLines ? (
              <span className="badge green">
                <span className="dot" />
                Front matter present
              </span>
            ) : (
              <span className="badge red">
                <span className="dot" />
                Missing front matter
              </span>
            )}
          </div>
          <div className="note" style={{ marginTop: 14 }}>
            Saving writes the working copy to blob. Publishing to the running app comes later
            (requirements §5.3).
          </div>
        </div>
      </div>
    </>
  )
}
