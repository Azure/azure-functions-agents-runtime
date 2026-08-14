// A lightweight, accessible searchable dropdown: a button that opens a panel with
// a filter box and the matching options. Controlled + prop-driven so it can
// replace any native <select> across the portal.

import { useEffect, useId, useMemo, useRef, useState } from 'react'

export interface SearchOption {
  value: string
  label: string
  sublabel?: string
}

interface SearchableSelectProps {
  value: string
  onChange: (value: string) => void
  options: SearchOption[]
  placeholder?: string
  disabled?: boolean
  loading?: boolean
  ariaLabel?: string
}

export const SearchableSelect = ({
  value,
  onChange,
  options,
  placeholder = 'Select…',
  disabled = false,
  loading = false,
  ariaLabel,
}: SearchableSelectProps) => {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const rootRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listId = useId()

  const selected = options.find((o) => o.value === value)
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return options
    return options.filter(
      (o) =>
        o.label.toLowerCase().includes(q) ||
        o.value.toLowerCase().includes(q) ||
        (o.sublabel?.toLowerCase().includes(q) ?? false),
    )
  }, [options, query])

  // Close on outside click.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  // Focus the filter box when opening; reset the highlight as results change.
  useEffect(() => {
    if (open) inputRef.current?.focus()
  }, [open])
  useEffect(() => {
    setActive(0)
  }, [query, open])

  const choose = (v: string) => {
    onChange(v)
    setOpen(false)
    setQuery('')
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      setOpen(false)
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((a) => Math.min(a + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((a) => Math.max(a - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const opt = filtered[active]
      if (opt) choose(opt.value)
    }
  }

  return (
    <div className="ss" ref={rootRef}>
      <button
        type="button"
        className="ss-btn"
        disabled={disabled || loading}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={'ss-val' + (selected ? '' : ' ss-placeholder')}>
          {loading ? 'Loading…' : selected ? selected.label : placeholder}
        </span>
        <span className="ss-chev" aria-hidden="true">
          ▾
        </span>
      </button>
      {open && (
        <div className="ss-panel">
          <input
            ref={inputRef}
            className="ss-search"
            type="text"
            value={query}
            placeholder="Search…"
            aria-label="Filter options"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <div className="ss-list" role="listbox" id={listId}>
            {filtered.length === 0 && <div className="ss-empty">No matches</div>}
            {filtered.map((o, i) => (
              <button
                type="button"
                key={o.value}
                role="option"
                aria-selected={o.value === value}
                className={'ss-opt' + (i === active ? ' active' : '') + (o.value === value ? ' selected' : '')}
                onMouseEnter={() => setActive(i)}
                onClick={() => choose(o.value)}
              >
                {o.label}
                {o.sublabel && <span className="ss-sub">{o.sublabel}</span>}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
