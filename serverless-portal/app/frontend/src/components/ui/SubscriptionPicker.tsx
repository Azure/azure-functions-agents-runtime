// Controlled Azure-subscription dropdown. Fully prop-driven (no app/store
// coupling) so it can be reused on any page or lifted into another app.

export interface SubscriptionOption {
  id: string
  name: string
}

interface SubscriptionPickerProps {
  subscriptions: SubscriptionOption[]
  value: string
  onChange: (id: string) => void
  loading?: boolean
  error?: boolean
  label?: string
  disabled?: boolean
}

export const SubscriptionPicker = ({
  subscriptions,
  value,
  onChange,
  loading = false,
  error = false,
  label = 'Subscription',
  disabled = false,
}: SubscriptionPickerProps) => (
  <label className="sub-picker" title="Azure subscription">
    <span className="sub-picker-label">{label}</span>
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled || loading || error || subscriptions.length === 0}
    >
      {loading && <option value="">Loading…</option>}
      {error && <option value="">Unavailable</option>}
      {!loading &&
        !error &&
        subscriptions.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name}
          </option>
        ))}
    </select>
  </label>
)
