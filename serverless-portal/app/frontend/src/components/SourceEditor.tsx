// Shared source viewer/editor used by the AI App and agent detail pages. Loads
// a deployed source file (or a saved portal draft), lets the user edit it, and
// saves edits to the portal working copy. Publishing a draft to the live app is
// a separate "Deploy edits" step. Extracted so multiple pages reuse it.

import { useEffect, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Tooltip } from '@coreai/fluentui-react'
import { CopyRegular, CheckmarkRegular } from '@fluentui/react-icons'

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
}: {
  queryKey: unknown[]
  load: () => Promise<{ content: string; source: string }>
  save: (content: string) => Promise<unknown>
  fallback: string
  renderActions?: (s: { source: string; dirty: boolean }) => ReactNode
  onSaved?: () => void
}) {
  const qc = useQueryClient()
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
        <p className="muted" style={{ fontSize: 12, margin: '0 0 8px' }}>
          The deployed source couldn’t be read (permission or plan) — start from here; saving stores a
          portal draft.
        </p>
      )}
      <textarea
        className="editor"
        spellCheck={false}
        value={value}
        onChange={(e) => setText(e.target.value)}
        aria-label="Source editor"
      />
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
