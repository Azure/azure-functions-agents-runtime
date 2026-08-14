import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { useDeployJob, DeploymentStatus } from '../deploy'
import { useIdentity } from '../identity'
import { queryKeys, readAgentsSnapshot } from '../query'
import { DeployTargetPicker } from '../components/ui'
import { DraftEditor } from '../components/SourceEditor'
import { AddCapability } from '../components/AddCapability'
import {
  type Draft,
  loadDraft,
  saveDraft,
  clearDraft,
  slugify,
  composeAgentMd,
  defaultAppName,
  defaultResourceGroup,
  sanitizeAppName,
  FLEX_REGIONS,
} from '../agentDraft'

// Review the app generated on the Create page, then deploy it to a Function App
// to try it — and, once deployed, connect a GitHub repo. The generated agent
// lives in sessionStorage; deploying provisions the app and (only after that)
// GitHub connect + the Foundry access grant appear inline via DeploymentStatus.
export default function DraftAppPage() {
  const navigate = useNavigate()
  const { selected, subscriptions, identity } = useIdentity()
  const [draft, setDraft] = useState<Draft>(loadDraft)

  // Auto-save the draft for this session on every change.
  useEffect(() => {
    saveDraft(draft)
  }, [draft])

  // Suggest resource names once so the user doesn't have to type them. Only
  // fills blanks, so an edited name survives reloads.
  useEffect(() => {
    setDraft((d) => {
      const appName = d.newApp.appName || defaultAppName(d.name)
      const resourceGroup =
        d.newApp.rgMode === 'new' && !d.newApp.resourceGroup
          ? defaultResourceGroup(d.name)
          : d.newApp.resourceGroup
      if (appName === d.newApp.appName && resourceGroup === d.newApp.resourceGroup) return d
      return { ...d, newApp: { ...d.newApp, appName, resourceGroup } }
    })
  }, [])

  // Function App name availability (names share the global *.azurewebsites.net namespace).
  const [nameStatus, setNameStatus] = useState<'idle' | 'checking' | 'available' | 'taken' | 'error'>('idle')
  const [nameMsg, setNameMsg] = useState('')

  // Capabilities: once the user adds MCP tools / triggers, the agent is saved as a
  // backend draft (so the shared AddCapability flow can edit it) and the app name is
  // locked so those drafts stay attached to it.
  const [capsEnabled, setCapsEnabled] = useState(false)
  const [materializing, setMaterializing] = useState(false)
  const [capErr, setCapErr] = useState('')

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }))
  const targetSub = draft.targetSubscription || selected

  const snapshot = useMemo(() => readAgentsSnapshot(targetSub), [targetSub])
  const { data, isFetching: appsLoading } = useQuery({
    queryKey: queryKeys.liveAgents(targetSub),
    queryFn: () => api.liveAgents(targetSub),
    enabled: !!targetSub,
    staleTime: Infinity,
    refetchOnMount: false,
    initialData: snapshot?.data,
    initialDataUpdatedAt: snapshot?.updatedAt,
  })
  const apps = data?.apps ?? []

  const { data: rgData, isFetching: rgLoading } = useQuery({
    queryKey: ['resource-groups', targetSub],
    queryFn: () => api.listResourceGroups(targetSub),
    enabled: !!targetSub && draft.target === 'new',
    staleTime: 5 * 60 * 1000,
  })
  const resourceGroups = rgData?.resourceGroups ?? []

  const selectTargetSub = (sub: string) =>
    setDraft((d) => ({
      ...d,
      targetSubscription: sub,
      existingApp: '',
      newApp: { ...d.newApp, resourceGroup: '' },
    }))

  const slug = slugify(draft.name)
  const fileName = `${slug}.agent.md`
  const composed = composeAgentMd(draft)
  const previewMd = draft.mdOverride ?? composed

  const deployJob = useDeployJob()
  const deployedAppName = draft.target === 'existing' ? draft.existingApp : draft.newApp.appName
  const deployedRg =
    draft.target === 'existing'
      ? (apps.find((a) => a.name === draft.existingApp)?.resourceGroup ?? '')
      : draft.newApp.resourceGroup

  // The app the capability drafts attach to (the new app being created).
  const capApp = sanitizeAppName(draft.newApp.appName).replace(/-+$/g, '')
  const capRg = draft.newApp.resourceGroup

  // Save the generated agent as a backend draft so the shared AddCapability flow
  // (triggers, MCP tools, skills) can read and edit it. Locks the app name.
  const enableCapabilities = async () => {
    if (!capApp || !draft.name.trim()) return
    setMaterializing(true)
    setCapErr('')
    try {
      await api.saveAgentDefinition({ subscription: targetSub, app: capApp, name: draft.name, content: previewMd })
      setCapsEnabled(true)
    } catch (e) {
      setCapErr((e as Error).message)
    } finally {
      setMaterializing(false)
    }
  }

  const runDeploy = () => {
    const target =
      draft.target === 'existing'
        ? {
            kind: 'existing' as const,
            app: draft.existingApp,
            resourceGroup: apps.find((a) => a.name === draft.existingApp)?.resourceGroup ?? '',
          }
        : {
            kind: 'new' as const,
            appName: sanitizeAppName(draft.newApp.appName).replace(/-+$/g, ''),
            resourceGroup: draft.newApp.resourceGroup,
            region: draft.newApp.region,
            foundryEndpoint: draft.foundryEndpoint,
            foundryModel: draft.foundryModel,
            // In pick mode we know the Foundry account, so the backend can
            // auto-grant the new app's identity access to it.
            ...(draft.foundryMode === 'pick' && draft.foundryAccount
              ? {
                  foundryAccount: {
                    subscription: draft.foundrySubscription || selected,
                    resourceGroup: draft.foundryResourceGroup,
                    account: draft.foundryAccount,
                  },
                }
              : {}),
          }
    deployJob.deploy({ subscription: targetSub, agent: { fileName, content: previewMd }, target })
  }

  const nameValid = draft.name.trim().length > 0
  const targetValid =
    draft.target === 'existing'
      ? !!draft.existingApp
      : !!draft.newApp.appName && !!draft.newApp.resourceGroup && !!draft.newApp.region
  const foundryValid = !!draft.foundryModel && (draft.target === 'existing' || !!draft.foundryEndpoint)
  const canDeploy = nameValid && targetValid && foundryValid && !!selected && deployJob.phase !== 'running'

  // Reset the availability result whenever the app name changes.
  useEffect(() => {
    setNameStatus('idle')
    setNameMsg('')
  }, [draft.newApp.appName])

  const checkName = async () => {
    const name = draft.newApp.appName.trim()
    if (!name) return
    setNameStatus('checking')
    setNameMsg('')
    try {
      const r = await api.checkName({ subscription: targetSub, name })
      setNameStatus(r.available ? 'available' : 'taken')
      if (!r.available) setNameMsg(r.message || 'That name is already taken.')
    } catch (e) {
      setNameStatus('error')
      setNameMsg((e as Error).message)
    }
  }

  const startOver = () => {
    clearDraft()
    navigate('/create-agent')
  }

  // Arrived without a generated agent (e.g. a refresh after the tab was closed)
  // — guide the user back to describe one.
  const hasContent = !!draft.instructions.trim() || draft.mdOverride != null
  if (!hasContent) {
    return (
      <>
        <div className="breadcrumb">
          Home / <Link to={`/agents/${selected}`}>AI Apps</Link> / New app
        </div>
        <div className="page-title">
          <h1>No generated app yet</h1>
        </div>
        <div className="empty">
          Describe an agent to generate its code first. <Link to="/create-agent">Create an AI App →</Link>
        </div>
      </>
    )
  }

  return (
    <>
      <div className="breadcrumb">
        Home / <Link to={`/agents/${selected}`}>AI Apps</Link> / <Link to="/create-agent">Create</Link> / Review
      </div>
      <div className="page-title">
        <button className="btn ghost sm" onClick={() => navigate('/create-agent')} title="Back to describe">
          ← Back
        </button>
        <h1 className="mono">{fileName}</h1>
        <span className="badge blue">
          <span className="dot" /> {draft.foundryModel || 'no model'}
        </span>
      </div>
      <p className="page-sub">
        Review the generated app, then deploy it to try it — or, once it’s deployed, connect a GitHub repo.
        This draft is kept only for this browser session.
      </p>

      <div className="grid cols-2" style={{ alignItems: 'start' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div className="card">
            <h3>Agent</h3>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Name</label>
              <input
                type="text"
                value={draft.name}
                placeholder="support-triage"
                disabled={capsEnabled}
                onChange={(e) => set('name', e.target.value)}
              />
              <div className="hint">
                File <span className="mono">{fileName}</span> and route slug.
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h3 className="mono" style={{ margin: 0 }}>
                {fileName}
              </h3>
              {capsEnabled ? (
                <span className="badge blue">draft</span>
              ) : draft.mdOverride != null ? (
                <button
                  className="btn sm"
                  onClick={() => set('mdOverride', null)}
                  title="Recompose from the generated instructions"
                >
                  ↺ Reset
                </button>
              ) : (
                <span className="badge gray">generated</span>
              )}
            </div>
            {capsEnabled ? (
              <DraftEditor
                key={`agent:${capApp}:${draft.name}`}
                queryKey={['agentDefinition', targetSub, capApp, draft.name]}
                load={() =>
                  api.getAgentDefinition({
                    subscription: targetSub,
                    app: capApp,
                    resourceGroup: capRg,
                    name: draft.name,
                  })
                }
                save={(content) =>
                  api.saveAgentDefinition({ subscription: targetSub, app: capApp, name: draft.name, content })
                }
                fallback={previewMd}
              />
            ) : (
              <textarea
                className="editor"
                spellCheck={false}
                value={previewMd}
                onChange={(e) => set('mdOverride', e.target.value)}
                aria-label="Agent definition"
              />
            )}
          </div>

          {draft.target === 'new' &&
            (!capsEnabled ? (
              <div className="card">
                <div className="card-head">
                  <h3 style={{ margin: 0 }}>Capabilities</h3>
                </div>
                <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
                  Add MCP tools, triggers, connectors, or skills — generated with your Foundry model and
                  shipped when you deploy. Enabling locks the app name.
                </p>
                <button
                  className="btn"
                  disabled={materializing || !capApp || !draft.name.trim()}
                  onClick={() => void enableCapabilities()}
                >
                  {materializing ? 'Preparing…' : '＋ Add MCP tools & triggers'}
                </button>
                {capErr && (
                  <p className="muted" style={{ color: 'var(--red)', fontSize: 12, marginTop: 8 }}>
                    {capErr}
                  </p>
                )}
              </div>
            ) : (
              <AddCapability
                subscription={targetSub}
                resourceGroup={capRg}
                app={capApp}
                agentName={draft.name}
              />
            ))}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div className="card">
            <h3>Deploy to try it</h3>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              Choose where to run it. Creating a new app provisions an Azure Functions Flex Consumption app
              and reuses your Foundry model.
            </p>
            <div className="field">
              <label>Subscription</label>
              <select value={targetSub} onChange={(e) => selectTargetSub(e.target.value)}>
                {subscriptions.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </div>
            <DeployTargetPicker
              value={{ mode: draft.target, existingApp: draft.existingApp, newApp: draft.newApp }}
              onChange={(patch) =>
                setDraft((d) => ({
                  ...d,
                  ...(patch.mode !== undefined ? { target: patch.mode } : {}),
                  ...(patch.existingApp !== undefined ? { existingApp: patch.existingApp } : {}),
                }))
              }
              onNewApp={(patch) =>
                setDraft((d) => ({
                  ...d,
                  newApp: {
                    ...d.newApp,
                    ...patch,
                    ...(patch.appName !== undefined ? { appName: sanitizeAppName(patch.appName) } : {}),
                  },
                }))
              }
              apps={apps}
              appsLoading={appsLoading}
              resourceGroups={resourceGroups}
              rgLoading={rgLoading}
              regions={FLEX_REGIONS}
              modelHint={draft.foundryModel}
              lockAppName={capsEnabled}
            />
            {draft.target === 'new' && (
              <div className="toolbar" style={{ marginTop: 10 }}>
                <button
                  className="btn sm"
                  onClick={() => void checkName()}
                  disabled={!draft.newApp.appName.trim() || nameStatus === 'checking'}
                >
                  {nameStatus === 'checking' ? 'Checking…' : 'Check name availability'}
                </button>
                {nameStatus === 'available' && (
                  <span className="badge green">
                    <span className="dot" /> Available
                  </span>
                )}
                {nameStatus === 'taken' && (
                  <span className="muted" style={{ color: 'var(--red)', fontSize: 12 }}>
                    {nameMsg || 'Taken, try another name'}
                  </span>
                )}
                {nameStatus === 'error' && (
                  <span className="muted" style={{ fontSize: 12 }}>Couldn't check right now</span>
                )}
              </div>
            )}
            <div className="toolbar" style={{ marginTop: 14 }}>
              <button className="btn primary" disabled={!canDeploy} onClick={runDeploy}>
                {deployJob.phase === 'running'
                  ? 'Deploying…'
                  : draft.target === 'new'
                    ? '🚀 Create app & deploy'
                    : '🚀 Deploy to app'}
              </button>
              <button className="btn" onClick={startOver}>
                Start over
              </button>
              {!foundryValid && (
                <span className="muted" style={{ fontSize: 12 }}>
                  Set a Foundry project on the Model step (← Back) to fill the endpoint.
                </span>
              )}
              {foundryValid && !nameValid && (
                <span className="muted" style={{ fontSize: 12 }}>Enter an agent name.</span>
              )}
              {foundryValid && nameValid && !targetValid && (
                <span className="muted" style={{ fontSize: 12 }}>Choose or configure a Function App.</span>
              )}
            </div>
            <p className="muted" style={{ fontSize: 12, marginTop: 10, marginBottom: 0 }}>
              🐙 Connecting a GitHub repo becomes available here once the app is deployed.
            </p>
          </div>

          <DeploymentStatus
            phase={deployJob.phase}
            result={deployJob.result}
            portalUrl={deployJob.portalUrl}
            message={deployJob.message}
            grant={
              draft.foundryMode === 'pick' && draft.foundryAccount
                ? {
                    subscription: draft.foundrySubscription || selected,
                    resourceGroup: draft.foundryResourceGroup,
                    account: draft.foundryAccount,
                    tenantId: identity?.user?.tenantId,
                  }
                : undefined
            }
            github={
              deployedAppName && deployedRg
                ? { subscription: targetSub, resourceGroup: deployedRg, app: deployedAppName }
                : undefined
            }
          />

          {deployJob.phase === 'deployed' && deployedAppName && (
            <div className="card">
              <div className="card-head">
                <h3 style={{ margin: 0 }}>✓ Deployed</h3>
              </div>
              <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
                <Link to={`/apps/${encodeURIComponent(targetSub)}/${encodeURIComponent(deployedAppName)}`}>
                  Open the app →
                </Link>{' '}
                to manage its agents, MCP servers, tools/triggers, and code.
              </p>
            </div>
          )}
        </div>
      </div>
    </>
  )
}
