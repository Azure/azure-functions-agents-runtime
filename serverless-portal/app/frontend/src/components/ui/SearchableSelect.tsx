// A filterable single-select built as a purpose-built popover. Trigger matches
// its container width; the popover sits directly beneath (or above, when
// clipped) the trigger with a fixed max-height and a dedicated search input.
// Keeps the controlled, prop-driven API (value = option value, onChange(value))
// so it can replace any native <select> across the portal without call-site
// changes.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

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

const POPOVER_MAX_HEIGHT = 320
const POPOVER_MIN_WIDTH = 240

interface PopoverRect {
  left: number
  top: number
  width: number
  maxHeight: number
  above: boolean
}

// Compute the popover rect. `preferAbove` is captured at open time so we don't
// flip direction mid-scroll — that was a major flicker source.
function computeRect(trigger: HTMLElement, preferAbove?: boolean): PopoverRect {
  const r = trigger.getBoundingClientRect()
  const gap = 4
  const spaceBelow = window.innerHeight - r.bottom - gap
  const spaceAbove = r.top - gap
  const above =
    preferAbove !== undefined
      ? preferAbove
      : spaceBelow < 200 && spaceAbove > spaceBelow
  const maxHeight = Math.min(POPOVER_MAX_HEIGHT, Math.max(160, above ? spaceAbove : spaceBelow))
  const width = Math.max(POPOVER_MIN_WIDTH, r.width)
  const left = Math.min(Math.max(8, r.left), window.innerWidth - width - 8)
  const top = above ? r.top - gap - maxHeight : r.bottom + gap
  return { left, top, width, maxHeight, above }
}

function rectEqual(a: PopoverRect | null, b: PopoverRect | null): boolean {
  if (!a || !b) return false
  return (
    Math.abs(a.left - b.left) < 0.5 &&
    Math.abs(a.top - b.top) < 0.5 &&
    Math.abs(a.width - b.width) < 0.5 &&
    a.maxHeight === b.maxHeight &&
    a.above === b.above
  )
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
  // Whether the popover renders above the trigger — captured at open time so
  // subsequent scroll repositions don't flip direction mid-interaction.
  const aboveRef = useRef<boolean | undefined>(undefined)
  // Guard: distinguish keyboard-driven activeIndex changes (should scroll into
  // view) from parent re-render churn (should NOT scroll).
  const keyboardMoveRef = useRef(false)

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
      aboveRef.current = undefined
      triggerRef.current?.focus({ preventScroll: true })
    },
    [onChange],
  )

  const openPopover = useCallback(() => {
    if (disabled || loading) return
    if (triggerRef.current) {
      const r = computeRect(triggerRef.current)
      aboveRef.current = r.above
      setRect(r)
    }
    setOpen(true)
    setActiveIndex(Math.max(0, options.findIndex((o) => o.value === value)))
    setQuery('')
  }, [disabled, loading, options, value])

  // Reposition on scroll/resize while open, honouring the captured direction so
  // the popover doesn't flip mid-scroll. Skip re-renders when the rect hasn't
  // meaningfully changed (a common cause of visible flicker).
  useLayoutEffect(() => {
    if (!open) return
    let raf = 0
    const reposition = () => {
      if (raf) return
      raf = requestAnimationFrame(() => {
        raf = 0
        if (!triggerRef.current) return
        const next = computeRect(triggerRef.current, aboveRef.current)
        setRect((prev) => (rectEqual(prev, next) ? prev : next))
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
        aboveRef.current = undefined
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        setOpen(false)
        aboveRef.current = undefined
        triggerRef.current?.focus({ preventScroll: true })
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

  // Clamp active index when the filter narrows past it, without setting state
  // during render.
  useEffect(() => {
    if (activeIndex >= filtered.length && filtered.length > 0) setActiveIndex(0)
  }, [filtered.length, activeIndex])

  // Only scroll the active option into view when the change came from the
  // keyboard — hover-driven changes were causing the list to visibly jump
  // under the cursor.
  useEffect(() => {
    if (!open || !listRef.current) return
    if (!keyboardMoveRef.current) return
    keyboardMoveRef.current = false
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
      keyboardMoveRef.current = true
      setActiveIndex((i) => Math.min(filtered.length - 1, i + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      keyboardMoveRef.current = true
      setActiveIndex((i) => Math.max(0, i - 1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const target = filtered[activeIndex]
      if (target) commit(target.value)
    } else if (e.key === 'Home') {
      e.preventDefault()
      keyboardMoveRef.current = true
      setActiveIndex(0)
    } else if (e.key === 'End') {
      e.preventDefault()
      keyboardMoveRef.current = true
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

      {open && rect && createPortal(
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
        </div>,
        document.body,
      )}
    </div>
  )
}

