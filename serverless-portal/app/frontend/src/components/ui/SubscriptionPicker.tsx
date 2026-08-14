// Controlled Azure-subscription dropdown. Fully prop-driven (no app/store
// coupling) so it can be reused on any page or lifted into another app.

import { SearchableSelect } from './SearchableSelect'

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
    <SearchableSelect
      value={value}
      onChange={onChange}
      options={subscriptions.map((s) => ({ value: s.id, label: s.name }))}
      placeholder={error ? 'Unavailable' : 'Select a subscription…'}
      loading={loading}
      disabled={disabled || error || subscriptions.length === 0}
      ariaLabel="Azure subscription"
    />
  </label>
)
