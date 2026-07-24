// Small controlled form primitives shared by the component editors.
import { type ReactNode } from 'react'

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  )
}

export function TextInput({
  value,
  onChange,
  placeholder,
  mono,
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
  mono?: boolean
}) {
  return (
    <input
      type="text"
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      style={mono ? { fontFamily: '"Cascadia Code", Consolas, monospace', fontSize: 12.5 } : undefined}
    />
  )
}

export function TextArea({
  value,
  onChange,
  rows,
  placeholder,
  mono,
}: {
  value: string
  onChange: (v: string) => void
  rows?: number
  placeholder?: string
  mono?: boolean
}) {
  return (
    <textarea
      value={value}
      rows={rows ?? 4}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      style={mono ? { fontFamily: '"Cascadia Code", Consolas, monospace', fontSize: 12.5 } : undefined}
    />
  )
}

export function Select({
  value,
  onChange,
  options,
}: {
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
}) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  )
}

export function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  label: string
}) {
  return (
    <label className="toggle">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  )
}

// Editable list of short string chips (skills, tools, fields…).
export function StringList({
  values,
  onChange,
  placeholder,
}: {
  values: string[]
  onChange: (v: string[]) => void
  placeholder?: string
}) {
  return (
    <div>
      <div className="chip-list">
        {values.map((v, i) => (
          <span className="chip-editable" key={`${v}-${i}`}>
            {v}
            <button
              type="button"
              className="chip-x"
              title="Remove"
              onClick={() => onChange(values.filter((_, j) => j !== i))}
            >
              ×
            </button>
          </span>
        ))}
        {values.length === 0 && <span className="muted" style={{ fontSize: 12 }}>none</span>}
      </div>
      <input
        type="text"
        placeholder={placeholder ?? 'Type and press Enter to add…'}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            const v = (e.target as HTMLInputElement).value.trim()
            if (v && !values.includes(v)) onChange([...values, v])
            ;(e.target as HTMLInputElement).value = ''
          }
        }}
        style={{ marginTop: 6 }}
      />
    </div>
  )
}
