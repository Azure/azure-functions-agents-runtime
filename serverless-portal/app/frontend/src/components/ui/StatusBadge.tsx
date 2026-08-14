// Maps an app/deployment status string to a toned Badge. Unknown statuses fall
// back to a neutral gray pill so new backend states never render blank.

import { Badge, type BadgeTone } from './Badge'

const STATUS_MAP: Record<string, { tone: BadgeTone; label: string }> = {
  running: { tone: 'green', label: 'Running' },
  provisioning: { tone: 'amber', label: 'Provisioning' },
  deploying: { tone: 'amber', label: 'Deploying' },
  stopped: { tone: 'gray', label: 'Stopped' },
  draft: { tone: 'amber', label: 'Draft' },
  error: { tone: 'red', label: 'Error' },
}

export const StatusBadge = ({ status }: { status: string }) => {
  const entry = STATUS_MAP[status.toLowerCase()] ?? { tone: 'gray' as BadgeTone, label: status || 'Unknown' }
  return (
    <Badge tone={entry.tone} dot>
      {entry.label}
    </Badge>
  )
}
