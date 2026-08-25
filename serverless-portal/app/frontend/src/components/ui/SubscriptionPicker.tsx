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
  refreshing?: boolean
  onRetry?: () => void
  label?: string
  disabled?: boolean
}

export const SubscriptionPicker = ({
  subscriptions,
  value,
  onChange,
  loading = false,
  error = false,
  refreshing = false,
  onRetry,
  label = 'Subscription',
  disabled = false,
}: SubscriptionPickerProps) => (
  <label className="sub-picker" title={error ? 'Subscription list could not be refreshed' : 'Azure subscription'}>
    <span className="sub-picker-label">{label}</span>
    <span className="sub-picker-control">
      <SearchableSelect
        value={value}
        onChange={onChange}
        options={subscriptions.map((s) => ({ value: s.id, label: s.name }))}
        placeholder={error ? 'Subscriptions unavailable' : 'Select a subscription…'}
        loading={loading}
        disabled={disabled || subscriptions.length === 0}
        ariaLabel="Azure subscription"
      />
      {error && onRetry && (
        <button type="button" className="sub-picker-retry" disabled={refreshing} onClick={onRetry} title="Retry loading subscriptions" aria-label="Retry loading subscriptions">
          {refreshing ? '…' : '↻'}
        </button>
      )}
    </span>
  </label>
)
