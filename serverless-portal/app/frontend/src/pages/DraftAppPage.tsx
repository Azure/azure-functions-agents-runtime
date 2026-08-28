import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Button } from '@coreai/fluentui-react'
import { api, type LiveAgent, type LiveAgentApp, type LiveDiscovery } from '../api'
import { OutlookConnectionsPanel } from '../components/OutlookConnectionsPanel'
import { useDeployJob, DeploymentStatus } from '../deploy'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot, writeAgentsSnapshot } from '../query'
import { CreationSteps, DeployTargetPicker, Icon, SearchableSelect } from '../components/ui'
import {
  type Draft,
  clearDraft,
  composeAgentMd,
  defaultAppName,
  defaultResourceGroup,
  FLEX_REGIONS,
  loadDraft,
  sanitizeAppName,
  saveDraft,
  slugify,
} from '../agentDraft'

type PreparationState = 'idle' | 'running' | 'prepared' | 'error'

function targetPreparationKey(draft: Draft, subscription: string): string {
  return [
    subscription,
    draft.newApp.appName,
    draft.newApp.resourceGroup,
    draft.newApp.region,
    draft.foundrySubscription,
    draft.foundryAccount,
    draft.foundryEndpoint,
    draft.foundryModel,
  ].join('|')
}

function hasOutlookServer(content?: string | null): boolean {
  if (!content) return false
  try {
    const source = JSON.parse(content) as { servers?: Record<string, unknown> }
    return Object.prototype.hasOwnProperty.call(source.servers ?? {}, 'office365-outlook')
  } catch {
    return false
  }
}

export default function DraftAppPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const { selected, subscriptions, identity } = useIdentity()
  const [draft, setDraft] = useState<Draft>(loadDraft)
  const [nameStatus, setNameStatus] = useState<'idle' | 'checking' | 'available' | 'taken' | 'error'>('idle')
  const [nameMessage, setNameMessage] = useState('')
  const [preparationState, setPreparationState] = useState<PreparationState>('idle')
  const [preparationMessage, setPreparationMessage] = useState('')
  const [preparationPortalUrl, setPreparationPortalUrl] = useState('')
  const preparationAttempt = useRef(0)
  const cachedRef = useRef(false)

  useEffect(() => saveDraft(draft), [draft])

  useEffect(() => {
    setDraft((current) => {
      const appName = current.newApp.appName || defaultAppName(current.name)
      const resourceGroup =
        current.newApp.rgMode === 'new' && !current.newApp.resourceGroup
          ? defaultResourceGroup(current.name)
          : current.newApp.resourceGroup
      if (appName === current.newApp.appName && resourceGroup === current.newApp.resourceGroup) return current
      return { ...current, newApp: { ...current.newApp, appName, resourceGroup } }
    })
  }, [])

  const targetSubscription = draft.targetSubscription || selected
  const preparationKey = targetPreparationKey(draft, targetSubscription)
  const appPrepared = draft.target === 'new' && draft.preparedTargetKey === preparationKey && !!draft.preparationId
  const snapshot = useMemo(() => readAgentsSnapshot(targetSubscription), [targetSubscription])
  const { data, isFetching: appsLoading } = useQuery({
    queryKey: queryKeys.liveAgents(targetSubscription),
    queryFn: () => api.liveAgents(targetSubscription),
    enabled: !!targetSubscription,
    staleTime: Infinity,
    refetchOnMount: false,
    initialData: snapshot?.data,
    initialDataUpdatedAt: snapshot?.updatedAt,
  })
  const apps = data?.apps ?? []

  const { data: resourceGroupData, isFetching: resourceGroupsLoading } = useQuery({
    queryKey: ['resource-groups', targetSubscription],
    queryFn: () => api.listResourceGroups(targetSubscription),
    enabled: !!targetSubscription && draft.target === 'new',
    staleTime: 5 * 60 * 1000,
  })
  const resourceGroups = resourceGroupData?.resourceGroups ?? []

  const resetPreparation = () => {
    preparationAttempt.current += 1
    setPreparationState('idle')
    setPreparationMessage('')
    setPreparationPortalUrl('')
  }

  const updateTargetSubscription = (subscription: string) => {
    resetPreparation()
    setDraft((current) => ({
      ...current,
      targetSubscription: subscription,
      existingApp: '',
      newApp: { ...current.newApp, resourceGroup: '' },
      capabilitiesReviewed: false,
      outlookConfigured: false,
      preparationId: '',
      preparedTargetKey: '',
    }))
  }

  const updateTarget = (patch: Partial<Pick<Draft, 'target' | 'existingApp'>>) => {
    resetPreparation()
    setDraft((current) => ({
      ...current,
      ...patch,
      capabilitiesReviewed: false,
      outlookConfigured: false,
      preparationId: '',
      preparedTargetKey: '',
    }))
  }

  const updateNewTarget = (patch: Partial<Draft['newApp']>) => {
    resetPreparation()
    setDraft((current) => ({
      ...current,
      newApp: {
        ...current.newApp,
        ...patch,
        ...(patch.appName !== undefined ? { appName: sanitizeAppName(patch.appName) } : {}),
      },
      capabilitiesReviewed: false,
      outlookConfigured: false,
      preparationId: '',
      preparedTargetKey: '',
    }))
  }

  const skillSlug = slugify(draft.name)
  const fileName = `${skillSlug}.agent.md`
  const content = draft.mdOverride ?? composeAgentMd(draft)
  const selectedExistingApp = apps.find((app) => app.name === draft.existingApp)
  const targetName = draft.target === 'existing' ? draft.existingApp : draft.newApp.appName
  const targetResourceGroup = draft.target === 'existing'
    ? selectedExistingApp?.resourceGroup ?? ''
    : draft.newApp.resourceGroup

  const deployJob = useDeployJob()
  const targetValid =
    draft.target === 'existing'
      ? !!draft.existingApp && !!selectedExistingApp?.resourceGroup
      : !!draft.newApp.appName && !!draft.newApp.resourceGroup && !!draft.newApp.region
  const foundryValid = !!draft.foundryModel && (draft.target === 'existing' || !!draft.foundryEndpoint)
  const canReview = !!draft.name.trim() && targetValid && foundryValid && !!targetSubscription && draft.capabilitiesReviewed
  const canDeploy = canReview && deployJob.phase !== 'running'
  const requestedStep = searchParams.get('step')
  const step: 3 | 4 | 5 = requestedStep === '5' && canReview
    ? 5
    : requestedStep === '4' && targetValid ? 4 : 3

  const capabilityTargetLive = draft.target === 'existing' ? targetValid : appPrepared
  const { data: mcpSource } = useQuery({
    queryKey: ['source', targetSubscription, targetName, 'mcp.json'],
    queryFn: () => api.getSource({
      subscription: targetSubscription,
      app: targetName,
      resourceGroup: targetResourceGroup,
      path: 'mcp.json',
    }),
    enabled: step === 4 && capabilityTargetLive && !!targetName && !!targetResourceGroup,
    staleTime: Infinity,
    refetchOnMount: false,
  })

  const navigateToStep = (nextStep: number) => {
    if (nextStep === 1) navigate('/create-agent?step=1')
    else if (nextStep === 2) navigate('/create-agent?step=2')
    else if (nextStep === 3) setSearchParams({})
    else if (nextStep === 4 && targetValid) setSearchParams({ step: '4' })
    else if (nextStep === 5 && canReview) setSearchParams({ step: '5' })
  }

  useEffect(() => () => {
    preparationAttempt.current += 1
  }, [])

  useEffect(() => {
    if (!appPrepared) return
    setPreparationState('prepared')
    setPreparationMessage(`Function App "${draft.newApp.appName}" is ready for live setup.`)
  }, [appPrepared, draft.newApp.appName])

  const foundryAccountTarget = draft.foundryAccount
    ? {
        subscription: draft.foundrySubscription || selected,
        resourceGroup: draft.foundryResourceGroup,
        account: draft.foundryAccount,
      }
    : undefined

  const startAppPreparation = async () => {
    if (draft.target !== 'new' || !targetValid || preparationState === 'running') return
    const attempt = preparationAttempt.current + 1
    preparationAttempt.current = attempt
    const preparationId = draft.preparationId || crypto.randomUUID()
    setDraft((current) => ({ ...current, preparationId, capabilitiesReviewed: false }))
    setPreparationState('running')
    setPreparationMessage('Starting app preparation…')
    setPreparationPortalUrl('')
    try {
      const started = await api.startPrepareApp({
        subscription: targetSubscription,
        target: {
          kind: 'new',
          appName: sanitizeAppName(draft.newApp.appName).replace(/-+$/g, ''),
          resourceGroup: draft.newApp.resourceGroup,
          region: draft.newApp.region,
          foundryEndpoint: draft.foundryEndpoint,
          foundryModel: draft.foundryModel,
          preparationId,
          ...(foundryAccountTarget ? { foundryAccount: foundryAccountTarget } : {}),
        },
      })
      setPreparationPortalUrl(started.portalUrl ?? '')
      const deadline = Date.now() + 15 * 60 * 1000
      while (Date.now() < deadline && preparationAttempt.current === attempt) {
        await new Promise((resolve) => window.setTimeout(resolve, 2_000))
        const state = await api.getPrepareAppStatus(started.jobId)
        setPreparationMessage(state.message)
        if (state.portalUrl) setPreparationPortalUrl(state.portalUrl)
        if (state.status === 'prepared') {
          setDraft((current) => ({
            ...current,
            preparationId,
            preparedTargetKey: preparationKey,
            capabilitiesReviewed: false,
          }))
          setPreparationState('prepared')
          return
        }
        if (state.status === 'error') throw new Error(state.message)
      }
      if (preparationAttempt.current === attempt) throw new Error('App preparation timed out. Check the Azure portal.')
    } catch (error) {
      if (preparationAttempt.current !== attempt) return
      setPreparationState('error')
      setPreparationMessage((error as Error).message)
    }
  }

  const completeCapabilities = () => {
    const updated = { ...draft, capabilitiesReviewed: true }
    setDraft(updated)
    saveDraft(updated)
    setSearchParams({ step: '5' })
  }

  const handleConnectionStateChange = useCallback((configured: boolean) => {
    setDraft((current) => current.outlookConfigured === configured
      ? current
      : { ...current, outlookConfigured: configured })
  }, [])

  const runDeploy = () => {
    const target =
      draft.target === 'existing'
        ? {
            kind: 'existing' as const,
            app: draft.existingApp,
            resourceGroup: selectedExistingApp?.resourceGroup ?? '',
          }
        : appPrepared ? {
            kind: 'prepared' as const,
            appName: sanitizeAppName(draft.newApp.appName).replace(/-+$/g, ''),
            resourceGroup: draft.newApp.resourceGroup,
            preparationId: draft.preparationId,
            ...(foundryAccountTarget ? { foundryAccount: foundryAccountTarget } : {}),
          } : {
            kind: 'new' as const,
            appName: sanitizeAppName(draft.newApp.appName).replace(/-+$/g, ''),
            resourceGroup: draft.newApp.resourceGroup,
            region: draft.newApp.region,
            foundryEndpoint: draft.foundryEndpoint,
            foundryModel: draft.foundryModel,
            ...(foundryAccountTarget ? { foundryAccount: foundryAccountTarget } : {}),
          }
    deployJob.deploy({ subscription: targetSubscription, agent: { fileName, content }, target })
  }

  useEffect(() => {
    if (deployJob.phase !== 'deployed' || cachedRef.current || draft.target !== 'new' || !targetName) return
    cachedRef.current = true
    const host = (deployJob.result?.url ?? '').replace(/^https?:\/\//, '')
    const skillInApp = {
      name: draft.name,
      trigger: draft.trigger,
      builtinEndpoints: draft.builtinEndpoints,
      routes: [] as string[],
      supportingFunctions: [] as string[],
    }
    const appEntry: LiveAgentApp = {
      name: targetName,
      resourceGroup: draft.newApp.resourceGroup,
      location: draft.newApp.region,
      provider: draft.provider || 'foundry',
      defaultHostName: host,
      agents: [skillInApp],
      supportingFunctions: [],
    }
    const skillEntry: LiveAgent = {
      ...skillInApp,
      app: targetName,
      resourceGroup: draft.newApp.resourceGroup,
      region: draft.newApp.region,
      provider: draft.provider || 'foundry',
      defaultHostName: host,
    }
    const key = queryKeys.liveAgents(targetSubscription)
    const previous =
      queryClient.getQueryData<LiveDiscovery>(key) ??
      readAgentsSnapshot(targetSubscription)?.data ?? { subscriptionId: targetSubscription, apps: [], agents: [] }
    const next: LiveDiscovery = {
      ...previous,
      subscriptionId: targetSubscription,
      apps: [appEntry, ...previous.apps.filter((app) => app.name !== targetName)],
      agents: [
        skillEntry,
        ...previous.agents.filter((skill) => !(skill.app === targetName && skill.name === draft.name)),
      ],
    }
    queryClient.setQueryData(key, next)
    writeAgentsSnapshot(targetSubscription, next, Date.now())
  }, [deployJob.phase, deployJob.result, draft, queryClient, targetName, targetSubscription])

  useEffect(() => {
    setNameStatus('idle')
    setNameMessage('')
  }, [draft.newApp.appName])

  const checkName = async () => {
    if (!draft.newApp.appName.trim()) return
    setNameStatus('checking')
    setNameMessage('')
    try {
      const result = await api.checkName({ subscription: targetSubscription, name: draft.newApp.appName.trim() })
      setNameStatus(result.available ? 'available' : 'taken')
      if (!result.available) setNameMessage(result.message || 'That name is already taken.')
    } catch (error) {
      setNameStatus('error')
      setNameMessage((error as Error).message)
    }
  }

  const startOver = () => {
    clearDraft()
    navigate('/create-agent')
  }

  if (!draft.name.trim() || !draft.instructions.trim()) {
    return (
      <>
        <div className="breadcrumb">Home / <Link to={`/agents/${selected}`}>Hosted Skills</Link> / New Skill</div>
        <div className="page-title"><h1>No skill instructions yet</h1></div>
        <div className="empty">Add skill instructions first. <Link to="/create-agent">Create a new skill →</Link></div>
      </>
    )
  }

  return (
    <>
      <div className="breadcrumb">Home / <Link to={`/agents/${selected}`}>Hosted Skills</Link> / New Skill</div>
      <div className="create-flow">
        <div className="create-flow-header">
          <h1>Create a New Skill</h1>
          <p>Set up the app and its first Hosted Skill. You can change everything except the app name later.</p>
        </div>
        <CreationSteps
          current={step}
          completed={[true, true, targetValid, draft.capabilitiesReviewed, deployJob.phase === 'deployed']}
          available={[true, true, true, targetValid, canReview]}
          onNavigate={navigateToStep}
          disabled={preparationState === 'running'}
        />

      {step === 3 && (
        <div className="card create-flow-card">
          <h3>Choose a deployment target</h3>
          <p className="muted" style={{ marginTop: 0 }}>
            Add this skill to an existing Function App or create a new Azure Functions Flex Consumption app.
          </p>
          {appPrepared ? (
            <div className="prepared-target-summary">
              <span className="connection-service-icon"><Icon name="check" size={20} /></span>
              <div>
                <strong>{draft.newApp.appName}</strong>
                <span>{draft.newApp.resourceGroup} · {draft.newApp.region}</span>
                <small>Azure resources and managed identity are prepared. This target is locked to protect its live connections.</small>
              </div>
              <span className="badge green">Prepared</span>
            </div>
          ) : (
            <>
              <div className="field">
                <label>Subscription</label>
                <SearchableSelect
                  value={targetSubscription}
                  onChange={updateTargetSubscription}
                  options={subscriptions.map((subscription) => ({ value: subscription.id, label: subscription.name }))}
                  placeholder="Select a subscription…"
                  ariaLabel="Subscription"
                  disabled={preparationState === 'running'}
                />
              </div>
              <DeployTargetPicker
                value={{ mode: draft.target, existingApp: draft.existingApp, newApp: draft.newApp }}
                onChange={(patch) => updateTarget({
                  ...(patch.mode !== undefined ? { target: patch.mode } : {}),
                  ...(patch.existingApp !== undefined ? { existingApp: patch.existingApp } : {}),
                })}
                onNewApp={updateNewTarget}
                apps={apps}
                appsLoading={appsLoading}
                resourceGroups={resourceGroups}
                rgLoading={resourceGroupsLoading}
                regions={FLEX_REGIONS}
                modelHint={draft.foundryModel}
                disabled={preparationState === 'running'}
              />
              {draft.target === 'new' && (
                <div className="toolbar" style={{ marginTop: 12 }}>
                  <Button size="small" onClick={() => void checkName()} disabled={!draft.newApp.appName.trim() || nameStatus === 'checking' || preparationState === 'running'}>
                    {nameStatus === 'checking' ? 'Checking…' : 'Check name availability'}
                  </Button>
                  {nameStatus === 'available' && <span className="badge green"><span className="dot" /> Available</span>}
                  {nameStatus === 'taken' && <span className="muted" style={{ color: 'var(--red)', fontSize: 12 }}>{nameMessage}</span>}
                  {nameStatus === 'error' && <span className="muted" style={{ fontSize: 12 }}>{nameMessage || "Couldn't check right now"}</span>}
                </div>
              )}
            </>
          )}
          <div className="create-flow-actions">
            <Button onClick={() => navigateToStep(2)}>← Back</Button>
            <Button appearance="primary" disabled={!targetValid || !foundryValid} onClick={() => navigateToStep(4)}>Continue to tools &amp; connections →</Button>
          </div>
        </div>
      )}

      {step === 4 && (
        <div className="create-flow-card live-capability-step">
          <div className="creation-capability-heading">
            <div>
              <h3>Configure tools and connections</h3>
              <p className="muted">This step is optional. Configure Outlook now, or skip it and continue to review.</p>
            </div>
            <span className="badge gray">Optional</span>
          </div>

          {draft.target === 'new' && !appPrepared ? (
            <div className="prepare-app-panel">
              <span className="connection-service-icon"><Icon name="server" size={20} /></span>
              <div>
                <strong>Prepare {draft.newApp.appName}</strong>
                <p>Live connection setup needs the Function App's managed identity. Preparing creates the Azure infrastructure now; skill source is deployed only after final review.</p>
              </div>
            </div>
          ) : (
            <>
              <p className="muted live-capability-target">Changes are applied to <span className="mono">{targetName}</span> immediately.</p>
              <OutlookConnectionsPanel
                subscription={targetSubscription}
                resourceGroup={targetResourceGroup}
                app={targetName}
                mcpSourceState={mcpSource?.source ?? 'none'}
                hasOutlookMcp={hasOutlookServer(mcpSource?.content)}
                onConnectionStateChange={handleConnectionStateChange}
              />
            </>
          )}

          {preparationMessage && (
            <div className={`note ${preparationState === 'error' ? 'warn' : preparationState === 'prepared' ? 'ok' : ''}`} role="status">
              {preparationState === 'running' && <span className="gh-spin" />} {preparationMessage}{' '}
              {preparationPortalUrl && <a href={preparationPortalUrl} target="_blank" rel="noreferrer">View in Azure ↗</a>}
            </div>
          )}

          <div className="create-flow-actions">
            <Button onClick={() => navigateToStep(3)} disabled={preparationState === 'running'}>← Back</Button>
            <div className="toolbar">
              <Button onClick={completeCapabilities} disabled={preparationState === 'running'}>Skip for now</Button>
              {draft.target === 'new' && !appPrepared ? (
                <Button appearance="primary" onClick={() => void startAppPreparation()} disabled={preparationState === 'running'}>
                  {preparationState === 'running' ? 'Preparing app…' : preparationState === 'error' ? 'Retry preparation' : 'Prepare app & configure'}
                </Button>
              ) : draft.outlookConfigured ? (
                <Button appearance="primary" onClick={completeCapabilities}>Continue to review →</Button>
              ) : null}
            </div>
          </div>
        </div>
      )}

      {step === 5 && (
        <div className="create-flow-card">
          <div className="review-grid">
            <div><span>Skill</span><strong>{draft.name}</strong></div>
            <div><span>Model</span><strong>{draft.foundryModel}</strong></div>
            <div><span>Function App</span><strong>{targetName}</strong></div>
            <div><span>Subscription</span><strong>{subscriptions.find((subscription) => subscription.id === targetSubscription)?.name || targetSubscription}</strong></div>
          </div>
          <div className="card review-prompt">
            <div className="card-head"><h3 style={{ margin: 0 }}>Skill instructions</h3><span className="badge green"><span className="dot" /> Ready</span></div>
            <pre>{draft.instructions}</pre>
          </div>
          <div className="note review-capability-selection">
            <strong>Tools &amp; connections</strong><br />
            {draft.outlookConfigured
              ? 'Outlook MCP server is configured on the target Function App.'
              : 'Skipped. Tools and connections can be configured later.'}
          </div>
          <div className="note ok review-validation">
            <strong>Ready to deploy</strong><br />The model, skill instructions, and Function App target are configured.
          </div>
          <div className="create-flow-actions">
            <Button onClick={() => navigateToStep(4)} disabled={deployJob.phase === 'running'}>← Back</Button>
            <Button appearance="primary" disabled={!canDeploy} onClick={runDeploy} icon={<Icon name="rocket" size={14} />}>
              {deployJob.phase === 'running' ? 'Deploying…' : draft.target === 'new' && !appPrepared ? 'Create app and deploy' : 'Deploy skill'}
            </Button>
          </div>
          <DeploymentStatus
            phase={deployJob.phase}
            result={deployJob.result}
            portalUrl={deployJob.portalUrl}
            message={deployJob.message}
            grant={
              draft.foundryAccount
                ? {
                    subscription: draft.foundrySubscription || selected,
                    resourceGroup: draft.foundryResourceGroup,
                    account: draft.foundryAccount,
                    tenantId: identity?.user?.tenantId,
                  }
                : undefined
            }
          />
          {deployJob.phase === 'deployed' && (
            <div className="create-flow-actions">
              <Button onClick={startOver}>Create another skill</Button>
              <Button appearance="primary" onClick={() => navigate(`/agents/${targetSubscription}`)}>Open Hosted Skills</Button>
            </div>
          )}
        </div>
      )}
      </div>
    </>
  )
}
