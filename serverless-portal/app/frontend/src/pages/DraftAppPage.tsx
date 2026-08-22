import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Button } from '@coreai/fluentui-react'
import { api, type LiveAgent, type LiveAgentApp, type LiveDiscovery } from '../api'
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

export default function DraftAppPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const { selected, subscriptions, identity } = useIdentity()
  const [draft, setDraft] = useState<Draft>(loadDraft)
  const [nameStatus, setNameStatus] = useState<'idle' | 'checking' | 'available' | 'taken' | 'error'>('idle')
  const [nameMessage, setNameMessage] = useState('')
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

  const updateTargetSubscription = (subscription: string) =>
    setDraft((current) => ({
      ...current,
      targetSubscription: subscription,
      existingApp: '',
      newApp: { ...current.newApp, resourceGroup: '' },
    }))

  const skillSlug = slugify(draft.name)
  const fileName = `${skillSlug}.agent.md`
  const content = draft.mdOverride ?? composeAgentMd(draft)
  const selectedExistingApp = apps.find((app) => app.name === draft.existingApp)
  const targetName = draft.target === 'existing' ? draft.existingApp : draft.newApp.appName

  const deployJob = useDeployJob()
  const targetValid =
    draft.target === 'existing'
      ? !!draft.existingApp
      : !!draft.newApp.appName && !!draft.newApp.resourceGroup && !!draft.newApp.region
  const foundryValid = !!draft.foundryModel && (draft.target === 'existing' || !!draft.foundryEndpoint)
  const canReview = !!draft.name.trim() && targetValid && foundryValid && !!targetSubscription
  const canDeploy = canReview && deployJob.phase !== 'running'
  const step: 3 | 4 = searchParams.get('step') === '4' && canReview ? 4 : 3

  const navigateToStep = (nextStep: number) => {
    if (nextStep === 1) navigate('/create-agent?step=1')
    else if (nextStep === 2) navigate('/create-agent?step=2')
    else if (nextStep === 3) setSearchParams({})
    else if (nextStep === 4 && canReview) setSearchParams({ step: '4' })
  }

  const runDeploy = () => {
    const target =
      draft.target === 'existing'
        ? {
            kind: 'existing' as const,
            app: draft.existingApp,
            resourceGroup: selectedExistingApp?.resourceGroup ?? '',
          }
        : {
            kind: 'new' as const,
            appName: sanitizeAppName(draft.newApp.appName).replace(/-+$/g, ''),
            resourceGroup: draft.newApp.resourceGroup,
            region: draft.newApp.region,
            foundryEndpoint: draft.foundryEndpoint,
            foundryModel: draft.foundryModel,
            ...(draft.foundryAccount
              ? {
                  foundryAccount: {
                    subscription: draft.foundrySubscription || selected,
                    resourceGroup: draft.foundryResourceGroup,
                    account: draft.foundryAccount,
                  },
                }
              : {}),
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
        <div className="page-title"><h1>No generated skill yet</h1></div>
        <div className="empty">Generate a skill first. <Link to="/create-agent">Create a new skill →</Link></div>
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
          completed={[true, true, canReview, deployJob.phase === 'deployed']}
          available={[true, true, true, canReview]}
          onNavigate={navigateToStep}
        />

      {step === 3 && (
        <div className="card create-flow-card">
          <h3>Choose a deployment target</h3>
          <p className="muted" style={{ marginTop: 0 }}>
            Add this skill to an existing Function App or create a new Azure Functions Flex Consumption app.
          </p>
          <div className="field">
            <label>Subscription</label>
            <SearchableSelect
              value={targetSubscription}
              onChange={updateTargetSubscription}
              options={subscriptions.map((subscription) => ({ value: subscription.id, label: subscription.name }))}
              placeholder="Select a subscription…"
              ariaLabel="Subscription"
            />
          </div>
          <DeployTargetPicker
            value={{ mode: draft.target, existingApp: draft.existingApp, newApp: draft.newApp }}
            onChange={(patch) =>
              setDraft((current) => ({
                ...current,
                ...(patch.mode !== undefined ? { target: patch.mode } : {}),
                ...(patch.existingApp !== undefined ? { existingApp: patch.existingApp } : {}),
              }))
            }
            onNewApp={(patch) =>
              setDraft((current) => ({
                ...current,
                newApp: {
                  ...current.newApp,
                  ...patch,
                  ...(patch.appName !== undefined ? { appName: sanitizeAppName(patch.appName) } : {}),
                },
              }))
            }
            apps={apps}
            appsLoading={appsLoading}
            resourceGroups={resourceGroups}
            rgLoading={resourceGroupsLoading}
            regions={FLEX_REGIONS}
            modelHint={draft.foundryModel}
          />
          {draft.target === 'new' && (
            <div className="toolbar" style={{ marginTop: 12 }}>
              <Button size="small" onClick={() => void checkName()} disabled={!draft.newApp.appName.trim() || nameStatus === 'checking'}>
                {nameStatus === 'checking' ? 'Checking…' : 'Check name availability'}
              </Button>
              {nameStatus === 'available' && <span className="badge green"><span className="dot" /> Available</span>}
              {nameStatus === 'taken' && <span className="muted" style={{ color: 'var(--red)', fontSize: 12 }}>{nameMessage}</span>}
              {nameStatus === 'error' && <span className="muted" style={{ fontSize: 12 }}>{nameMessage || "Couldn't check right now"}</span>}
            </div>
          )}
          <div className="create-flow-actions">
            <Button onClick={() => navigateToStep(2)}>← Back</Button>
            <Button appearance="primary" disabled={!canReview} onClick={() => navigateToStep(4)}>Review and deploy →</Button>
          </div>
        </div>
      )}

      {step === 4 && (
        <div className="create-flow-card">
          <div className="review-grid">
            <div><span>Skill</span><strong>{draft.name}</strong></div>
            <div><span>Model</span><strong>{draft.foundryModel}</strong></div>
            <div><span>Function App</span><strong>{targetName}</strong></div>
            <div><span>Subscription</span><strong>{subscriptions.find((subscription) => subscription.id === targetSubscription)?.name || targetSubscription}</strong></div>
          </div>
          <div className="card review-prompt">
            <div className="card-head"><h3 style={{ margin: 0 }}>Generated prompt</h3><span className="badge green"><span className="dot" /> Ready</span></div>
            <pre>{draft.instructions}</pre>
          </div>
          <div className="note ok review-validation">
            <strong>Ready to deploy</strong><br />The model, skill prompt, and Function App target are configured.
          </div>
          <div className="create-flow-actions">
            <Button onClick={() => navigateToStep(3)} disabled={deployJob.phase === 'running'}>← Back</Button>
            <Button appearance="primary" disabled={!canDeploy} onClick={runDeploy} icon={<Icon name="rocket" size={14} />}>
              {deployJob.phase === 'running' ? 'Deploying…' : draft.target === 'new' ? 'Create app and deploy' : 'Deploy skill'}
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
