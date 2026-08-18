// A filterable single-select built on the CoreAI/Fluent Combobox. Keeps the
// controlled, prop-driven API (value = option value, onChange(value)) so it can
// replace any native <select> across the portal without call-site changes.

import { useMemo, useState } from 'react'
import { Combobox, Option } from '@coreai/fluentui-react'

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
  const selected = options.find((o) => o.value === value)
  // `query` is the live filter text while the user is typing; `null` means "not
  // typing" so the input shows the selected option's label.
  const [query, setQuery] = useState<string | null>(null)

  const filtered = useMemo(() => {
    const q = (query ?? '').trim().toLowerCase()
    if (!q) return options
    return options.filter(
      (o) =>
        o.label.toLowerCase().includes(q) ||
        o.value.toLowerCase().includes(q) ||
        (o.sublabel?.toLowerCase().includes(q) ?? false),
    )
  }, [options, query])

  return (
    <Combobox
      value={query ?? selected?.label ?? ''}
      selectedOptions={value ? [value] : []}
      placeholder={loading ? 'Loading…' : placeholder}
      disabled={disabled || loading}
      aria-label={ariaLabel}
      freeform
      onOptionSelect={(_, data) => {
        onChange(data.optionValue ?? '')
        setQuery(null)
      }}
      onChange={(e) => setQuery(e.target.value)}
      onOpenChange={(_, data) => {
        // Revert any unselected typed text back to the current selection on close.
        if (!data.open) setQuery(null)
      }}
    >
      {filtered.length === 0 ? (
        <Option key="__none" value="__none" text="" disabled>
          No matches
        </Option>
      ) : (
        filtered.map((o) => (
          <Option key={o.value} value={o.value} text={o.label}>
            {o.sublabel ? (
              <span className="ss-opt-body">
                <span>{o.label}</span>
                <span className="ss-sub">{o.sublabel}</span>
              </span>
            ) : (
              o.label
            )}
          </Option>
        ))
      )}
    </Combobox>
  )
}
