import { useState } from 'react'
import { Button, Input } from '@coreai/fluentui-react'

import { Modal } from './Modal'
import { Icon } from './ui'

interface AppActionDialogProps {
  appName: string
  skillCount: number
  busy: boolean
  error: string
  pending?: boolean
  onClose: () => void
}

interface StopFunctionAppDialogProps extends AppActionDialogProps {
  onConfirm: () => void
}

interface DeleteFunctionAppDialogProps extends AppActionDialogProps {
  onConfirm: (confirmation: string) => void
}

export function StopFunctionAppDialog({
  appName,
  skillCount,
  busy,
  error,
  onClose,
  onConfirm,
}: StopFunctionAppDialogProps) {
  return (
    <Modal title="Stop Function App?" onClose={onClose} closeDisabled={busy} width={620}>
      <div className="app-lifecycle-impact">
        <Icon name="alert" size={20} />
        <div>
          <strong>{appName}</strong>
          <span>
            Stopping this Function App makes {skillCount} Hosted Skill{skillCount === 1 ? '' : 's'} unavailable.
          </span>
        </div>
      </div>
      <p className="muted app-lifecycle-copy">
        Requests will fail until an operator restarts the Function App in Azure. No app data or Azure resources will be deleted.
      </p>
      {error && <div className="gh-err" role="alert">{error}</div>}
      <div className="modal-actions">
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button className="danger-button" onClick={onConfirm} disabled={busy}>
          {busy ? 'Stopping…' : 'Stop Function App'}
        </Button>
      </div>
    </Modal>
  )
}

export function DeleteFunctionAppDialog({
  appName,
  skillCount,
  busy,
  error,
  pending = false,
  onClose,
  onConfirm,
}: DeleteFunctionAppDialogProps) {
  const [confirmation, setConfirmation] = useState('')
  const matches = confirmation.trim() === appName
  return (
    <Modal title="Delete Function App?" onClose={onClose} closeDisabled={busy} width={660}>
      <div className="app-lifecycle-impact is-destructive">
        <Icon name="alert" size={20} />
        <div>
          <strong>{appName}</strong>
          <span>
            This permanently deletes the Function App and removes {skillCount} Hosted Skill{skillCount === 1 ? '' : 's'} from this portal.
          </span>
        </div>
      </div>
      <div className="app-lifecycle-preserved">
        <strong>These resources will not be deleted:</strong>
        <ul>
          <li>Resource group, storage account, and App Service plan</li>
          <li>Application Insights and Log Analytics</li>
          <li>Foundry resources and model deployments</li>
          <li>GitHub repositories, Connector Gateways, and Outlook connections</li>
        </ul>
      </div>
      <div className="field app-lifecycle-confirmation">
        <label htmlFor="delete-function-app-confirmation">Type <strong>{appName}</strong> exactly to confirm</label>
        <Input
          id="delete-function-app-confirmation"
          value={confirmation}
          onChange={(_, data) => setConfirmation(data.value)}
          disabled={busy || pending}
          autoComplete="off"
          aria-describedby="delete-function-app-confirmation-hint"
        />
        <div id="delete-function-app-confirmation-hint" className="hint" aria-live="polite">
          {pending
            ? 'Azure accepted the deletion. Refresh Hosted Skills to confirm completion.'
            : confirmation && !matches ? 'The app name does not match.' : 'Only the Function App will be deleted.'}
        </div>
      </div>
      {error && <div className="gh-err" role="alert">{error}</div>}
      <div className="modal-actions">
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button
          className="danger-button"
          icon={<Icon name="trash" size={15} />}
          onClick={() => onConfirm(confirmation.trim())}
          disabled={busy || pending || !matches}
        >
          {busy ? 'Deleting…' : 'Delete Function App'}
        </Button>
      </div>
    </Modal>
  )
}
