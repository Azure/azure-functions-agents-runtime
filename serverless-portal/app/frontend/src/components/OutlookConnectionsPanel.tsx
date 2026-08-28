import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Input } from '@coreai/fluentui-react'

import {
  api,
  ApiError,
  type ConnectionTest,
  type ConnectionSetup,
  type ConnectionSetupSource,
  type OutlookConnection,
  type OutlookConnectionCandidate,
} from '../api'
import { Modal } from './Modal'
import { Icon } from './ui'
import { useIdentity } from '../identity'

type SetupMode = 'create' | 'existing'
const SUCCESS_NOTICE_DURATION_MS = 6_000

interface OutlookConnectionsPanelProps {
  subscription: string
  resourceGroup: string
  app: string
  mcpSourceState: 'draft' | 'deployed' | 'none'
  hasOutlookMcp: boolean
}

function ConnectionStatus({ value }: { value: OutlookConnection['status'] }) {
  const tone = value === 'Connected' ? 'green' : value === 'Expired' ? 'red' : 'amber'
  return <span className={`badge ${tone}`}><span className="dot" /> {value}</span>
}

function removalErrorMessage(error: unknown) {
  if (!(error instanceof ApiError)) return (error as Error).message
  const cleanup = error.data.cleanup as { sourceDraft?: string; appSetting?: string } | undefined
  const manualRecovery = cleanup && ['rollback_failed', 'restore_failed'].some(
    (outcome) => Object.values(cleanup).includes(outcome),
  )
  return manualRecovery
    ? `${error.message} Source or Function App configuration could not be restored; review mcp.json drafts and O365_MCP_SERVER_URL manually.`
    : error.message
}

export function OutlookConnectionsPanel({
  subscription,
  resourceGroup,
  app,
  mcpSourceState,
  hasOutlookMcp,
}: OutlookConnectionsPanelProps) {
  const queryClient = useQueryClient()
  const { subscriptions, subscriptionsLoading, subscriptionError, refreshSubscriptions } = useIdentity()
  const context = { subscription, resourceGroup, app }
  const connectionKey = ['connections', subscription, resourceGroup, app]
  const [connectorSubscription, setConnectorSubscription] = useState(subscription)
  const candidateKey = ['connectionCandidates', subscription, resourceGroup, app, connectorSubscription]
  const [wizardOpen, setWizardOpen] = useState(false)
  const [step, setStep] = useState(1)
  const [mode, setMode] = useState<SetupMode>('create')
  const [displayName, setDisplayName] = useState('Outlook reports')
  const [selectedId, setSelectedId] = useState('')
  const [configured, setConfigured] = useState<OutlookConnection | null>(null)
  const [setupSource, setSetupSource] = useState<ConnectionSetupSource | null>(null)
  const [removalTarget, setRemovalTarget] = useState<OutlookConnection | null>(null)
  const [testResult, setTestResult] = useState<ConnectionTest | null>(null)
  const [actionError, setActionError] = useState('')
  const [actionMessage, setActionMessage] = useState('')

  const connectionsQuery = useQuery({
    queryKey: connectionKey,
    queryFn: () => api.listConnections(context),
    staleTime: 15_000,
    retry: false,
  })
  const candidatesQuery = useQuery({
    queryKey: candidateKey,
    queryFn: () => api.listOutlookConnectionCandidates({ ...context, connectorSubscription }),
    enabled: wizardOpen && step === 2 && mode === 'existing' && !!connectorSubscription,
    staleTime: 15_000,
    retry: false,
  })
  const connections = connectionsQuery.data?.connections ?? []
  const candidates = candidatesQuery.data?.connections ?? []
  const outlookConfigured = connections.some((connection) => connection.service === 'Office 365 Outlook')

  useEffect(() => {
    if (!actionMessage) return
    const timeout = window.setTimeout(() => setActionMessage(''), SUCCESS_NOTICE_DURATION_MS)
    return () => window.clearTimeout(timeout)
  }, [actionMessage])

  useEffect(() => {
    if (!testResult?.ok) return
    const timeout = window.setTimeout(() => setTestResult(null), SUCCESS_NOTICE_DURATION_MS)
    return () => window.clearTimeout(timeout)
  }, [testResult])

  useEffect(() => {
    setConnectorSubscription(subscription)
    setSelectedId('')
  }, [subscription])

  const refreshSourceQueries = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: ['source', subscription, app, 'mcp.json'] }),
    queryClient.invalidateQueries({ queryKey: ['sourceList', subscription, resourceGroup, app] }),
  ])

  const completeSetup = async ({ connection, source }: ConnectionSetup) => {
    setConfigured(connection)
    setSetupSource(source)
    setStep(3)
    setActionError('')
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: connectionKey }),
      refreshSourceQueries(),
    ])
  }

  const createConnection = useMutation({
    mutationFn: () => api.createOutlookConnection({ ...context, displayName }),
    onSuccess: (result) => void completeSetup(result),
    onError: (error) => setActionError((error as Error).message),
  })
  const attachConnection = useMutation({
    mutationFn: (connectionId: string) => api.attachOutlookConnection({
      ...context,
      connectionId,
      connectorSubscription,
    }),
    onSuccess: (result) => void completeSetup(result),
    onError: (error) => setActionError((error as Error).message),
  })
  const testConnection = useMutation({
    mutationFn: (connection: OutlookConnection) => api.testConnection({ ...context, id: connection.id }),
    onSuccess: async (result) => {
      setTestResult(result)
      setActionError('')
      await queryClient.invalidateQueries({ queryKey: connectionKey })
    },
    onError: (error) => setActionError((error as Error).message),
  })
  const repairConnection = useMutation({
    mutationFn: (connection: OutlookConnection) => connection.source === 'Existing'
      ? api.attachOutlookConnection({
          ...context,
          connectionId: connection.id,
          connectorSubscription: connection.subscriptionId,
        })
      : api.createOutlookConnection({ ...context, displayName: connection.displayName }),
    onSuccess: async (result) => {
      setTestResult(null)
      setActionError('')
      setActionMessage(result.source.deploymentRequired
        ? 'Outlook source is ready as an mcp.json draft. Select Deploy to make it available to Hosted Skills.'
        : 'Outlook source is already deployed; no deployment is required.')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: connectionKey }),
        refreshSourceQueries(),
      ])
    },
    onError: (error) => setActionError((error as Error).message),
  })
  const removeConnection = useMutation({
    mutationFn: (connection: OutlookConnection) => api.deleteOutlookConnection({ ...context, id: connection.id }),
    onSuccess: async (result) => {
      queryClient.setQueryData<{ connections: OutlookConnection[] }>(connectionKey, { connections: [] })
      setRemovalTarget(null)
      setTestResult(null)
      setActionError('')
      setActionMessage([
        result.source === 'Existing' ? 'Outlook connection removed from this app.' : 'Outlook connection deleted.',
        result.cleanup.azure === 'deletion_pending' ? 'Azure is finishing resource deletion.' : '',
        result.sourceDraftChanged ? 'The office365-outlook block was removed in an mcp.json draft; deploy to publish that source change.' : '',
      ].filter(Boolean).join(' '))
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: connectionKey, refetchType: 'none' }),
        queryClient.invalidateQueries({ queryKey: ['source', subscription, app, 'mcp.json'] }),
        queryClient.invalidateQueries({ queryKey: ['sourceList', subscription, resourceGroup, app] }),
      ])
    },
    onError: (error) => setActionError(removalErrorMessage(error)),
  })

  const openWizard = () => {
    setStep(1)
    setMode('create')
    setConnectorSubscription(subscription)
    setSelectedId('')
    setConfigured(null)
    setSetupSource(null)
    setTestResult(null)
    setActionError('')
    setActionMessage('')
    setWizardOpen(true)
  }
  const beginAuthorization = (connection: OutlookConnection) => {
    setActionError('')
    setActionMessage(
      `In Connector Namespace, open ${connection.displayName}, choose Authorize, and complete Microsoft sign-in. Then return here and select Check status.`,
    )
  }
  const refresh = async () => {
    setActionError('')
    setTestResult(null)
    await connectionsQuery.refetch()
  }

  return (
    <section className="connection-panel">
      <div className="skill-section-head connection-panel-head">
        <div>
          <h3>Connections</h3>
          <p>App-shared service connections available to this Hosted Skill.</p>
        </div>
        <div className="connection-actions">
          <Button
            icon={<Icon name="refresh" size={15} />}
            onClick={() => void refresh()}
            disabled={connectionsQuery.isFetching}
          >
            {connectionsQuery.isFetching ? 'Refreshing...' : 'Refresh'}
          </Button>
          <Button
            appearance="primary"
            icon={<Icon name="plus" size={16} />}
            onClick={openWizard}
            title="Add a service connection"
          >
            Add connection
          </Button>
        </div>
      </div>

      {connectionsQuery.error && <div className="gh-err">{(connectionsQuery.error as Error).message}</div>}
      {actionError && <div className="gh-err" style={{ marginBottom: 12 }}>{actionError}</div>}
      {actionMessage && <div className="note ok connection-transient-notice" style={{ marginBottom: 12 }}>{actionMessage}</div>}
      {mcpSourceState === 'draft' && (
        <div className="note warn connection-deploy-required">
          <Icon name="alert" size={17} />
          <span><strong>Deploy required.</strong> <span className="mono">mcp.json</span> has unpublished changes. Select <strong>Deploy</strong> at the top of this page to update Hosted Skills.</span>
        </div>
      )}
      {connections.length > 0 && !hasOutlookMcp && mcpSourceState !== 'draft' && (
        <div className="note warn connection-deploy-required">
          <Icon name="alert" size={17} />
          <span><strong>Outlook source configuration is missing.</strong> Create the required <span className="mono">mcp.json</span> draft, then deploy it.</span>
          <Button size="small" onClick={() => repairConnection.mutate(connections[0])} disabled={repairConnection.isPending}>
            {repairConnection.isPending ? 'Preparing draft...' : 'Configure source'}
          </Button>
        </div>
      )}

      <div className="table-wrap connections-table">
        <table>
          <thead>
            <tr>
              <th>Connection</th>
              <th>Source</th>
              <th>Location</th>
              <th>Operation</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {connectionsQuery.isLoading ? (
              <tr><td colSpan={6} className="connection-empty-cell">Loading connections...</td></tr>
            ) : connections.length === 0 ? (
              <tr><td colSpan={6} className="connection-empty-cell">No Outlook connection configured for this app.</td></tr>
            ) : connections.map((connection) => (
              <tr key={connection.id}>
                <td>
                  <div className="connection-service">
                    <span className="connection-service-icon"><Icon name="mail" size={20} /></span>
                    <span>
                      <span className="cell-title">{connection.displayName}</span>
                      <span className="cell-sub">{connection.authenticatedUser || 'Microsoft sign-in required'}</span>
                    </span>
                  </div>
                </td>
                <td>{connection.source}</td>
                <td>
                  <span className="cell-title">{connection.resourceGroup}</span>
                  <span className="cell-sub connection-location">{connection.gatewayName}</span>
                  <span className="cell-sub connection-location mono" title="Connector subscription">{connection.subscriptionId}</span>
                </td>
                <td><span className="badge blue">Send email</span></td>
                <td>
                  <ConnectionStatus value={connection.status} />
                  {connection.providerErrorCode && (
                    <span className="cell-sub connection-detail">Azure: {connection.providerErrorCode}</span>
                  )}
                  {connection.detail && <span className="cell-sub connection-detail">{connection.detail}</span>}
                </td>
                <td>
                  <div className="connection-actions">
                    {!connection.infrastructureReady ? (
                      <Button
                        size="small"
                        onClick={() => repairConnection.mutate(connection)}
                        disabled={repairConnection.isPending}
                      >
                        {repairConnection.isPending
                          ? 'Repairing...'
                          : connection.source === 'Existing' ? 'Retry attachment' : 'Retry setup'}
                      </Button>
                    ) : (
                      <a
                        className="btn sm"
                        href={connection.portalUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={() => beginAuthorization(connection)}
                      >
                        {connection.authorizationRequired
                          ? 'Authorize'
                          : connection.status === 'Connected' ? 'Reconnect' : 'Open Connector portal'}
                      </a>
                    )}
                    <Button
                      size="small"
                      onClick={() => testConnection.mutate(connection)}
                      disabled={testConnection.isPending}
                    >
                      Check status
                    </Button>
                    <Button
                      size="small"
                      icon={<Icon name="trash" size={14} />}
                      className="connection-remove-button"
                      onClick={() => {
                        setActionError('')
                        setActionMessage('')
                        setRemovalTarget(connection)
                      }}
                    >
                      {connection.source === 'Existing' ? 'Remove from app' : 'Delete'}
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {connections.find((connection) => connection.authorizationRequired) && (() => {
        const connection = connections.find((candidate) => candidate.authorizationRequired)!
        return (
          <div className="note warn connection-auth-remediation">
            <Icon name="alert" size={18} />
            <div>
              <strong>Authorize Outlook to finish setup</strong>
              <span>
                Azure reports {connection.providerErrorCode || 'Unauthenticated'}: {connection.providerErrorMessage || 'This connection is not authenticated.'}
              </span>
              <span>Open Connector Namespace, select <strong>{connection.displayName}</strong>, choose <strong>Authorize</strong>, complete Microsoft sign-in, then return and select <strong>Check status</strong>.</span>
            </div>
            <a
              className="btn sm"
              href={connection.portalUrl}
              target="_blank"
              rel="noopener noreferrer"
              onClick={() => beginAuthorization(connection)}
            >
              Authorize in Connector Namespace
            </a>
          </div>
        )
      })()}

      {testResult && (
        <div className={`connection-test ${testResult.ok ? 'is-ok connection-transient-notice' : 'is-failed'}`}>
          <strong>{testResult.ok ? 'Configuration checks passed' : 'Connection needs attention'}</strong>
          <div className="connection-checks">
            {testResult.checks.map((check) => (
              <span key={check.name}>
                <Icon name={check.ok ? 'check' : 'alert'} size={14} />
                {check.name}{!check.ok && check.detail ? `: ${check.detail}` : ''}
              </span>
            ))}
          </div>
        </div>
      )}

      {wizardOpen && (
        <Modal title="Add Outlook connection" onClose={() => setWizardOpen(false)} width={720}>
          <div className="connection-steps" aria-label={`Step ${step} of 3`}>
            {['Source', 'Configure', 'Finish'].map((label, index) => (
              <span
                key={label}
                className={`${step >= index + 1 ? 'active' : ''}${step === index + 1 ? ' current' : ''}`}
              >
                {index + 1}. {label}
              </span>
            ))}
          </div>

          {step === 1 && (
            <>
              {outlookConfigured && (
                <div className="note warn" style={{ marginBottom: 14 }}>
                  <strong>Office 365 Outlook is already configured.</strong> Remove the current Outlook connection before selecting another one. Additional connector types will remain independent.
                </div>
              )}
              <div className="connection-mode-control" role="group" aria-label="Connection source">
                <button type="button" disabled={outlookConfigured} className={mode === 'create' ? 'active' : ''} aria-pressed={mode === 'create'} onClick={() => setMode('create')}>
                  Create new
                </button>
                <button
                  type="button"
                  disabled={outlookConfigured}
                  className={mode === 'existing' ? 'active' : ''}
                  aria-pressed={mode === 'existing'}
                  onClick={() => {
                    setMode('existing')
                    setStep(2)
                  }}
                >
                  Use existing
                </button>
              </div>
              <div className="note connection-mode-note">
                {mode === 'create'
                  ? 'Creates a Connector Gateway and Office 365 Outlook connection for this app.'
                  : 'Selects an Office 365 Outlook connection from any subscription available to your current sign-in.'}
              </div>
              <div className="modal-actions">
                <Button appearance="primary" disabled={outlookConfigured} onClick={() => setStep(2)}>Continue</Button>
              </div>
            </>
          )}

          {step === 2 && mode === 'create' && (
            <>
              <div className="field">
                <label>Connection name</label>
                <Input value={displayName} maxLength={80} onChange={(_, data) => setDisplayName(data.value)} />
              </div>
              <div className="permission-row">
                <Icon name="mail" size={18} />
                <span><strong>Send email</strong><small>Creates Azure resources and exposes only Outlook SendEmailV2.</small></span>
                <Icon name="check" size={17} />
              </div>
              {actionError && <div className="gh-err">{actionError}</div>}
              <div className="modal-actions">
                <Button onClick={() => setStep(1)}>Back</Button>
                <Button
                  appearance="primary"
                  disabled={!displayName.trim() || createConnection.isPending}
                  onClick={() => createConnection.mutate()}
                >
                  {createConnection.isPending ? 'Creating Azure resources...' : 'Create and configure'}
                </Button>
              </div>
            </>
          )}

          {step === 2 && mode === 'existing' && (
            <>
              <div className="field">
                <label htmlFor="connector-subscription">Connector subscription</label>
                <select
                  id="connector-subscription"
                  value={connectorSubscription}
                  disabled={subscriptionsLoading || attachConnection.isPending}
                  onChange={(event) => {
                    setConnectorSubscription(event.target.value)
                    setSelectedId('')
                    setActionError('')
                  }}
                >
                  {!subscriptions.some((candidate) => candidate.id === subscription) && (
                    <option value={subscription}>{subscription} (Function App)</option>
                  )}
                  {subscriptions.map((candidate) => (
                    <option key={candidate.id} value={candidate.id}>
                      {candidate.name} ({candidate.id}){candidate.id === subscription ? ' - Function App' : ''}
                    </option>
                  ))}
                </select>
                <div className="hint">This selection does not change the Function App subscription.</div>
              </div>
              {subscriptionError && (
                <div className="note warn">
                  Could not refresh subscriptions: {subscriptionError}{' '}
                  <button className="link-button" onClick={() => void refreshSubscriptions()}>Retry</button>
                </div>
              )}
              <p className="muted connection-picker-intro">
                The selected gateway and connection will not be changed. This app adds two access policies and one send-only MCP configuration in the connector subscription, then stores its endpoint in the Function App subscription.
              </p>
              {candidatesQuery.isLoading && <p className="muted">Loading existing Office 365 connections...</p>}
              {candidatesQuery.error && <div className="gh-err">{(candidatesQuery.error as Error).message}</div>}
              {candidatesQuery.data?.partial && <div className="note warn">Some Connector Gateways could not be read and are not shown.</div>}
              {!candidatesQuery.isLoading && !candidatesQuery.error && (
                <div className="table-wrap connection-candidate-table">
                  <table>
                    <thead><tr><th /><th>Connection</th><th>Gateway</th><th>Microsoft sign-in</th></tr></thead>
                    <tbody>
                      {candidates.length === 0 ? (
                        <tr><td colSpan={4} className="connection-empty-cell">No eligible Office 365 connections found in the selected subscription.</td></tr>
                      ) : candidates.map((candidate) => (
                        <CandidateRow
                          key={candidate.id}
                          candidate={candidate}
                          selected={selectedId === candidate.id}
                          onSelect={() => setSelectedId(candidate.id)}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {actionError && <div className="gh-err" style={{ marginTop: 12 }}>{actionError}</div>}
              <div className="modal-actions">
                <Button onClick={() => setStep(1)}>Back</Button>
                <Button onClick={() => void candidatesQuery.refetch()} disabled={candidatesQuery.isFetching}>Refresh list</Button>
                <Button
                  appearance="primary"
                  disabled={!selectedId || attachConnection.isPending}
                  onClick={() => attachConnection.mutate(selectedId)}
                >
                  {attachConnection.isPending ? 'Configuring app...' : 'Use selected connection'}
                </Button>
              </div>
            </>
          )}

          {step === 3 && configured && (
            <>
              <div className="connection-ready">
                <Icon name="check" size={24} />
                <div>
                  <strong>{configured.source === 'Existing' ? 'Existing connection configured' : 'Connection created and configured'}</strong>
                  <span>
                    {setupSource?.deploymentRequired
                      ? 'An mcp.json draft is ready. Select Done, then Deploy to make Outlook available to Hosted Skills.'
                      : configured.status === 'Connected'
                      ? 'Azure authentication, app access, and the Function App endpoint are configured.'
                      : 'Complete Microsoft sign-in, then return to check the connection status.'}
                  </span>
                </div>
              </div>
              <div className="modal-actions">
                {configured.status !== 'Connected' && (
                  <a
                    className="btn"
                    href={configured.portalUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={() => beginAuthorization(configured)}
                  >
                    Authorize in Connector Namespace
                  </a>
                )}
                <Button appearance="primary" onClick={() => { setWizardOpen(false); void refresh() }}>Done</Button>
              </div>
            </>
          )}
        </Modal>
      )}

      {removalTarget && (
        <Modal
          title={removalTarget.source === 'Existing' ? 'Remove Outlook from this app?' : 'Delete Outlook connection?'}
          onClose={() => !removeConnection.isPending && setRemovalTarget(null)}
          width={620}
        >
          <div className="note warn connection-removal-warning">
            <strong>{removalTarget.displayName}</strong>
            <span>
              {removalTarget.source === 'Existing'
                ? 'The shared Outlook connection and its Microsoft sign-in will be preserved.'
                : 'The app-owned Connector Gateway and its child resources will be deleted.'}
            </span>
          </div>
          <ul className="connection-removal-list">
            {removalTarget.source === 'Existing' ? (
              <>
                <li>Delete this app's send-only MCP configuration from <strong>{removalTarget.gatewayName}</strong>.</li>
                <li>Delete this Function App identity's access policy.</li>
                <li>Keep the shared gateway, Outlook connection, Microsoft sign-in, and signed-in user policy.</li>
              </>
            ) : (
              <li>Delete the app-owned Connector Gateway <strong>{removalTarget.gatewayName}</strong>, including its Outlook connection, access policies, and MCP configuration.</li>
            )}
            <li>Remove <span className="mono">O365_MCP_SERVER_URL</span> from the Function App settings.</li>
            <li>Save an <span className="mono">mcp.json</span> draft with only <span className="mono">office365-outlook</span> removed. Other MCP servers remain unchanged.</li>
          </ul>
          {actionError && <div className="gh-err">{actionError}</div>}
          <div className="modal-actions">
            <Button onClick={() => setRemovalTarget(null)} disabled={removeConnection.isPending}>Cancel</Button>
            <Button
              appearance="primary"
              className="danger-button"
              icon={<Icon name="trash" size={15} />}
              disabled={removeConnection.isPending}
              onClick={() => removeConnection.mutate(removalTarget)}
            >
              {removeConnection.isPending
                ? 'Removing...'
                : removalTarget.source === 'Existing' ? 'Remove from app' : 'Delete connection'}
            </Button>
          </div>
        </Modal>
      )}
    </section>
  )
}

function CandidateRow({
  candidate,
  selected,
  onSelect,
}: {
  candidate: OutlookConnectionCandidate
  selected: boolean
  onSelect: () => void
}) {
  return (
    <tr className={selected ? 'selected' : ''} onClick={onSelect}>
      <td>
        <input
          type="radio"
          name="outlook-connection"
          checked={selected}
          onChange={onSelect}
          aria-label={`Select ${candidate.displayName}`}
        />
      </td>
      <td><span className="cell-title">{candidate.displayName}</span><span className="cell-sub connection-location">{candidate.connectionName}</span></td>
      <td><span className="cell-title">{candidate.resourceGroup}</span><span className="cell-sub connection-location">{candidate.gatewayName}</span></td>
      <td><span className={`badge ${candidate.status === 'Connected' ? 'green' : candidate.status === 'Expired' ? 'red' : 'amber'}`}>{candidate.status}</span>{candidate.authenticatedUser && <span className="cell-sub connection-location">{candidate.authenticatedUser}</span>}</td>
    </tr>
  )
}