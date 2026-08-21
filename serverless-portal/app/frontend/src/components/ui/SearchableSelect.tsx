// A filterable single-select built as a purpose-built popover. Trigger matches
// its container width; the popover sits directly beneath (or above, when
// clipped) the trigger with a fixed max-height and a dedicated search input.
// Keeps the controlled, prop-driven API (value = option value, onChange(value))
// so it can replace any native <select> across the portal without call-site
// changes.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

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

// Number of options above which the popover renders the search input. Below
// this threshold search is redundant clutter.
const SEARCH_THRESHOLD = 6

// Popover geometry. The popover is anchored to the trigger, clamped to the
// viewport, and never wider than the trigger; a min-width guards very narrow
// triggers.
const POPOVER_MAX_HEIGHT = 320
const POPOVER_MIN_WIDTH = 240

interface PopoverRect {
  left: number
  top: number
  width: number
  maxHeight: number
  above: boolean
}

function computeRect(trigger: HTMLElement): PopoverRect {
  const r = trigger.getBoundingClientRect()
  const gap = 4
  const spaceBelow = window.innerHeight - r.bottom - gap
  const spaceAbove = r.top - gap
  const above = spaceBelow < 200 && spaceAbove > spaceBelow
  const maxHeight = Math.min(POPOVER_MAX_HEIGHT, Math.max(160, above ? spaceAbove : spaceBelow))
  const width = Math.max(POPOVER_MIN_WIDTH, r.width)
  const left = Math.min(Math.max(8, r.left), window.innerWidth - width - 8)
  const top = above ? r.top - gap - maxHeight : r.bottom + gap
  return { left, top, width, maxHeight, above }
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
  const selected = options.find((o) => o.value === value)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const [rect, setRect] = useState<PopoverRect | null>(null)

  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const popoverRef = useRef<HTMLDivElement | null>(null)
  const searchRef = useRef<HTMLInputElement | null>(null)
  const listRef = useRef<HTMLUListElement | null>(null)

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
  const showSearch = options.length >= SEARCH_THRESHOLD

  const commit = useCallback(
    (next: string) => {
      onChange(next)
      setOpen(false)
      setQuery('')
      triggerRef.current?.focus()
    },
    [onChange],
  )

  const openPopover = useCallback(() => {
    if (disabled || loading) return
    if (triggerRef.current) setRect(computeRect(triggerRef.current))
    setOpen(true)
    setActiveIndex(Math.max(0, options.findIndex((o) => o.value === value)))
    setQuery('')
  }, [disabled, loading, options, value])

  // Reposition on scroll/resize while open, so the popover tracks its trigger.
  useLayoutEffect(() => {
    if (!open) return
    let raf = 0
    const reposition = () => {
      if (raf) return
      raf = requestAnimationFrame(() => {
        raf = 0
        if (triggerRef.current) setRect(computeRect(triggerRef.current))
      })
    }
    window.addEventListener('scroll', reposition, { capture: true, passive: true })
    window.addEventListener('resize', reposition)
    return () => {
      if (raf) cancelAnimationFrame(raf)
      window.removeEventListener('scroll', reposition, true)
      window.removeEventListener('resize', reposition)
    }
  }, [open])

  // Dismiss on click outside / Escape.
  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as Node
      if (
        popoverRef.current &&
        !popoverRef.current.contains(target) &&
        triggerRef.current &&
        !triggerRef.current.contains(target)
      ) {
        setOpen(false)
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        setOpen(false)
        triggerRef.current?.focus()
      }
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // Focus the right element when the popover opens: search input when
  // rendered, otherwise the option list (so ArrowDown works immediately).
  useEffect(() => {
    if (!open) return
    if (showSearch) searchRef.current?.focus()
    else listRef.current?.focus()
  }, [open, showSearch])

  // Reset active index when the filter changes so ArrowDown starts sensibly.
  useEffect(() => {
    if (activeIndex >= filtered.length) setActiveIndex(0)
  }, [filtered, activeIndex])

  // Keep the active option visible in the scrollable list.
  useEffect(() => {
    if (!open || !listRef.current) return
    const active = listRef.current.querySelector<HTMLElement>('[data-active="true"]')
    active?.scrollIntoView({ block: 'nearest' })
  }, [open, activeIndex])

  const handleTriggerKey = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      openPopover()
    }
  }

  const handleListKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex((i) => Math.min(filtered.length - 1, i + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex((i) => Math.max(0, i - 1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const target = filtered[activeIndex]
      if (target) commit(target.value)
    } else if (e.key === 'Home') {
      e.preventDefault()
      setActiveIndex(0)
    } else if (e.key === 'End') {
      e.preventDefault()
      setActiveIndex(Math.max(0, filtered.length - 1))
    }
  }

  const triggerText = loading
    ? 'Loading…'
    : selected?.label
      ? selected.label
      : placeholder
  const isEmpty = !selected

  return (
    <div className="ss-root">
      <button
        ref={triggerRef}
        type="button"
        className={'ss-trigger' + (isEmpty ? ' is-empty' : '') + (open ? ' is-open' : '')}
        onClick={() => (open ? setOpen(false) : openPopover())}
        onKeyDown={handleTriggerKey}
        disabled={disabled || loading}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="ss-trigger-label">{triggerText}</span>
        <span className="ss-trigger-caret" aria-hidden>
          ▾
        </span>
      </button>

      {open && rect && (
        <div
          ref={popoverRef}
          className="ss-popover"
          role="dialog"
          style={{
            position: 'fixed',
            left: rect.left,
            top: rect.top,
            width: rect.width,
            maxHeight: rect.maxHeight,
          }}
        >
          {showSearch && (
            <div className="ss-search-wrap">
              <input
                ref={searchRef}
                className="ss-search"
                type="text"
                value={query}
                placeholder={`Search ${options.length} options`}
                onChange={(e) => {
                  setQuery(e.target.value)
                  setActiveIndex(0)
                }}
                onKeyDown={(e) => {
                  // Let arrows / Enter drive the option list even while focus
                  // stays in the search input.
                  if (['ArrowDown', 'ArrowUp', 'Enter', 'Home', 'End'].includes(e.key)) {
                    handleListKey(e)
                  }
                }}
                aria-label="Filter options"
              />
            </div>
          )}
          <ul
            ref={listRef}
            className="ss-list"
            role="listbox"
            tabIndex={-1}
            aria-label={ariaLabel}
            onKeyDown={handleListKey}
          >
            {filtered.length === 0 ? (
              <li className="ss-empty" role="presentation">
                No matches
              </li>
            ) : (
              filtered.map((o, i) => {
                const isActive = i === activeIndex
                const isSelected = o.value === value
                return (
                  <li
                    key={o.value}
                    className={'ss-opt' + (isActive ? ' is-active' : '') + (isSelected ? ' is-selected' : '')}
                    role="option"
                    aria-selected={isSelected}
                    data-active={isActive || undefined}
                    onMouseEnter={() => setActiveIndex(i)}
                    onMouseDown={(e) => {
                      // Prevent the search input from losing focus mid-click.
                      e.preventDefault()
                      commit(o.value)
                    }}
                  >
                    {o.sublabel ? (
                      <span className="ss-opt-body">
                        <span className="ss-opt-label">{o.label}</span>
                        <span className="ss-sub">{o.sublabel}</span>
                      </span>
                    ) : (
                      <span className="ss-opt-label">{o.label}</span>
                    )}
                    {isSelected && <span className="ss-check" aria-hidden>✓</span>}
                  </li>
                )
              })
            )}
          </ul>
        </div>
      )}
    </div>
  )
}

