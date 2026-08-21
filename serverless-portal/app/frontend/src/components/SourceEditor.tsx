// Shared source viewer/editor used by the Hosted Skills app and skill detail pages. Loads
// a deployed source file (or a saved portal draft), lets the user edit it, and
// saves edits to the portal working copy. Publishing a draft to the live app is
// a separate "Deploy edits" step. Extracted so multiple pages reuse it.

import { useEffect, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Tooltip } from '@coreai/fluentui-react'
import { CopyRegular, CheckmarkRegular } from '@fluentui/react-icons'
import { consentStorageAccess } from '../auth'
import { api, type ValidationIssue } from '../api'

export function CopyButton({ text, title }: { text: string; title: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <Tooltip content={title} relationship="label">
      <Button
        appearance="subtle"
        size="small"
        icon={copied ? <CheckmarkRegular /> : <CopyRegular />}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(text)
            setCopied(true)
            setTimeout(() => setCopied(false), 1200)
          } catch {
            /* clipboard unavailable */
          }
        }}
      >
        {copied ? 'Copied' : 'Copy'}
      </Button>
    </Tooltip>
  )
}

export function DraftEditor({
  queryKey,
  load,
  save,
  fallback,
  renderActions,
  onSaved,
  validationKind,
}: {
  queryKey: unknown[]
  load: () => Promise<{ content: string; source: string }>
  save: (content: string) => Promise<unknown>
  fallback: string
  renderActions?: (s: { source: string; dirty: boolean }) => ReactNode
  onSaved?: () => void
  // When set to 'agent.md', the current content is validated against the
  // runtime's schema on every keystroke (debounced) and errors are shown inline
  // below the editor. Prevents users from discovering typos on deploy.
  validationKind?: 'agent.md'
}) {
  const qc = useQueryClient()
  const [grantBusy, setGrantBusy] = useState(false)
  const { data, isLoading, error } = useQuery({
    queryKey,
    queryFn: load,
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnReconnect: false,
  })

  // Reset local edits whenever a fresh copy arrives (initial load, and after a
  // save invalidates + refetches).
  const [text, setText] = useState<string | null>(null)
  useEffect(() => {
    setText(null)
  }, [data])

  const saveMutation = useMutation({
    mutationFn: (content: string) => save(content),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey })
      onSaved?.()
    },
  })

  // Debounced schema validation for `.agent.md` — hits the backend which
  // parses the YAML frontmatter and checks it against the runtime's rules.
  const [validation, setValidation] = useState<{ errors: ValidationIssue[]; warnings: ValidationIssue[] } | null>(null)
  useEffect(() => {
    if (validationKind !== 'agent.md') return
    const current = text ?? data?.content ?? ''
    if (!current.trim()) {
      setValidation(null)
      return
    }
    const handle = setTimeout(() => {
      api
        .validateAgentMd(current)
        .then((r) => setValidation({ errors: r.errors, warnings: r.warnings }))
        .catch(() => setValidation(null))
    }, 350)
    return () => clearTimeout(handle)
  }, [text, data?.content, validationKind])

  if (isLoading) return <p className="muted">Loading…</p>
  if (error) return <p className="muted">Couldn’t load: {(error as Error).message}</p>

  const base = data?.content || fallback
  const value = text ?? base
  const dirty = value !== base
  const source = data?.source ?? 'none'
  const unreadable = !data?.content

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        {source === 'draft' ? (
          <span className="badge amber">
            <span className="dot" /> Draft (unpublished)
          </span>
        ) : source === 'deployed' ? (
          <span className="badge green">
            <span className="dot" /> Deployed source
          </span>
        ) : (
          <span className="badge gray">Source not readable</span>
        )}
        {dirty && (
          <span className="muted" style={{ fontSize: 12 }}>
            · unsaved changes
          </span>
        )}
        {!dirty && saveMutation.isSuccess && (
          <span className="muted" style={{ fontSize: 12 }}>
            · saved
          </span>
        )}
        <div style={{ flex: 1 }} />
        <Button
          appearance="subtle"
          size="small"
          onClick={() => setText(null)}
          disabled={!dirty || saveMutation.isPending}
          title="Discard unsaved changes"
        >
          Reset
        </Button>
        <Button
          appearance="primary"
          size="small"
          onClick={() => saveMutation.mutate(value)}
          disabled={!dirty || saveMutation.isPending}
        >
          {saveMutation.isPending ? 'Saving…' : 'Save draft'}
        </Button>
        {renderActions?.({ source, dirty })}
      </div>
      {unreadable && (
        <div style={{ margin: '0 0 8px' }}>
          <p className="muted" style={{ fontSize: 12, margin: '0 0 6px' }}>
            The deployed source couldn’t be read. Connected GitHub repos are read automatically; if your
            app’s storage requires it, grant read access below. Editing still saves a portal draft.
          </p>
          <Button
            size="small"
            disabled={grantBusy}
            onClick={async () => {
              setGrantBusy(true)
              try {
                const ok = await consentStorageAccess()
                if (ok) await qc.invalidateQueries({ queryKey })
              } finally {
                setGrantBusy(false)
              }
            }}
            title="Grant this portal read access to the app’s deployment storage, then retry"
          >
            {grantBusy ? 'Requesting access…' : 'Grant access & retry'}
          </Button>
        </div>
      )}
      <textarea
        className="editor"
        spellCheck={false}
        value={value}
        onChange={(e) => setText(e.target.value)}
        aria-label="Source editor"
      />
      {validationKind === 'agent.md' && validation && (validation.errors.length > 0 || validation.warnings.length > 0) && (
        <div className={'validation-panel' + (validation.errors.length > 0 ? ' has-errors' : ' has-warnings')}>
          <div className="validation-head">
            {validation.errors.length > 0 ? (
              <span className="badge red">
                {validation.errors.length} error{validation.errors.length > 1 ? 's' : ''}
              </span>
            ) : (
              <span className="badge amber">
                {validation.warnings.length} warning{validation.warnings.length > 1 ? 's' : ''}
              </span>
            )}
            <span className="muted" style={{ fontSize: 12 }}>
              These will block or degrade the next deploy. Fix before publishing.
            </span>
          </div>
          <ul className="validation-list">
            {validation.errors.map((e, i) => (
              <li key={`e${i}`} className="validation-error">
                <span className="mono">{e.path}</span> · {e.message}
              </li>
            ))}
            {validation.warnings.map((w, i) => (
              <li key={`w${i}`} className="validation-warning">
                <span className="mono">{w.path}</span> · {w.message}
              </li>
            ))}
          </ul>
        </div>
      )}
      {saveMutation.isError && (
        <p className="muted" style={{ color: 'var(--red)', fontSize: 12 }}>
          Save failed: {(saveMutation.error as Error).message}
        </p>
      )}
      <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
        Edits are saved to a portal-side working copy. Use <strong>Deploy edits</strong> above to publish
        this app with your saved changes.
      </p>
    </div>
  )
}
