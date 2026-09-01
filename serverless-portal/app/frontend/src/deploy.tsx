// Shared deployment job runner + status UI for the Create Agent and Agent
// Detail pages. Starting a deploy returns immediately with a job id and an
// Azure portal link, so the user can watch progress in the portal instead of
// waiting; the hook also polls the job to a terminal state in the background.

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  api,
  ApiError,
  type DeployResult,
  type DeployTarget,
  type GitHubStatus,
  type GitHubRepo,
  type GitHubConnectResult,
  type GitHubAppConnection,
  type GitHubPublishMode,
} from './api'
import { Button, Input } from '@coreai/fluentui-react'
import { AlertFilled, AlertRegular, DismissRegular } from '@fluentui/react-icons'
import { SearchableSelect } from './components/ui'

export type DeployPhase = 'idle' | 'running' | 'deployed' | 'error'

interface DeployJobValue {
  phase: DeployPhase
  result: DeployResult | null
  portalUrl?: string
  message: string
  deploy: (p: { subscription: string; agent: { fileName: string; content: string }; target: DeployTarget }) => Promise<void>
  redeploy: (p: { subscription: string; resourceGroup: string; app: string }) => Promise<void>
}

interface DeployContextValue {
  phase: DeployPhase
  result: DeployResult | null
  portalUrl?: string
  message: string
  owner: symbol | null
  notificationUnread: boolean
  completedAt: number | null
  markNotificationRead: () => void
  begin: (start: () => Promise<{ jobId: string; portalUrl?: string }>, owner: symbol) => Promise<void>
}

interface StoredDeployJob {
  jobId: string
  portalUrl?: string
}

const DeployContext = createContext<DeployContextValue | null>(null)
const ACTIVE_DEPLOY_KEY = 'serverless-portal:active-deploy'

function readActiveDeploy(): StoredDeployJob | null {
  try {
    const value = JSON.parse(localStorage.getItem(ACTIVE_DEPLOY_KEY) ?? 'null') as unknown
    if (!value || typeof value !== 'object' || !('jobId' in value) || typeof value.jobId !== 'string') return null
    return value as StoredDeployJob
  } catch {
    return null
  }
}

function storeActiveDeploy(job: StoredDeployJob | null): void {
  try {
    if (job) localStorage.setItem(ACTIVE_DEPLOY_KEY, JSON.stringify(job))
    else localStorage.removeItem(ACTIVE_DEPLOY_KEY)
  } catch {
    // Deployment polling still works when browser storage is unavailable.
  }
}

export function DeployProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [phase, setPhase] = useState<DeployPhase>('idle')
  const [result, setResult] = useState<DeployResult | null>(null)
  const [portalUrl, setPortalUrl] = useState<string | undefined>(undefined)
  const [message, setMessage] = useState<string>('')
  const [notificationVisible, setNotificationVisible] = useState(false)
  const [completedAt, setCompletedAt] = useState<number | null>(null)
  const [owner, setOwner] = useState<symbol | null>(null)
  const activeJob = useRef<string | null>(null)

  const poll = useCallback(async (jobId: string) => {
    const deadline = Date.now() + 15 * 60 * 1000
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 4000))
      if (activeJob.current !== jobId) return // superseded by a newer deploy
      let state: DeployResult
      try {
        state = await api.getDeployStatus(jobId)
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          activeJob.current = null
          storeActiveDeploy(null)
          setPhase('error')
          setResult({ status: 'error', message: 'Deployment status expired. Check the Azure portal.', files: [] })
          setCompletedAt(Date.now())
          setNotificationVisible(true)
          return
        }
        continue // transient poll error — keep trying
      }
      if (state.portalUrl) setPortalUrl(state.portalUrl)
      setMessage(state.message ?? '')
      if (state.status !== 'running') {
        activeJob.current = null
        storeActiveDeploy(null)
        setResult(state)
        setPhase(state.status === 'deployed' ? 'deployed' : 'error')
        setCompletedAt(Date.now())
        setNotificationVisible(true)
        if (state.status === 'deployed') {
          void Promise.all([
            queryClient.invalidateQueries({ queryKey: ['agentDefinition'] }),
            queryClient.invalidateQueries({ queryKey: ['source'] }),
            queryClient.invalidateQueries({ queryKey: ['sourceList'] }),
          ])
        }
        return
      }
    }
    if (activeJob.current === jobId) {
      setPhase('error')
      setResult({ status: 'error', message: 'Deploy timed out. Check the Azure portal.', files: [] })
      setCompletedAt(Date.now())
      activeJob.current = null
      storeActiveDeploy(null)
      setNotificationVisible(true)
    }
  }, [queryClient])

  const begin = useCallback(
    async (start: () => Promise<{ jobId: string; portalUrl?: string }>, jobOwner: symbol) => {
      if (activeJob.current) return
      activeJob.current = 'starting'
      setPhase('running')
      setResult(null)
      setPortalUrl(undefined)
      setMessage('Starting…')
      setOwner(jobOwner)
      setCompletedAt(null)
      setNotificationVisible(true)
      try {
        const started = await start()
        activeJob.current = started.jobId
        if (started.portalUrl) setPortalUrl(started.portalUrl)
        storeActiveDeploy({ jobId: started.jobId, portalUrl: started.portalUrl })
        void poll(started.jobId)
      } catch (e) {
        activeJob.current = null
        storeActiveDeploy(null)
        setPhase('error')
        setResult({ status: 'error', message: (e as Error).message, files: [] })
        setCompletedAt(Date.now())
        setNotificationVisible(true)
      }
    },
    [poll],
  )

  useEffect(() => {
    const stored = readActiveDeploy()
    if (!stored || activeJob.current) return
    activeJob.current = stored.jobId
    setPhase('running')
    setOwner(null)
    setPortalUrl(stored.portalUrl)
    setMessage('Resuming deployment status…')
    setCompletedAt(null)
    setNotificationVisible(true)
    void poll(stored.jobId)
    return () => {
      activeJob.current = null
    }
  }, [poll])

  const markNotificationRead = useCallback(() => setNotificationVisible(false), [])
  const value: DeployContextValue = {
    phase,
    result,
    portalUrl,
    message,
    owner,
    notificationUnread: notificationVisible,
    completedAt,
    markNotificationRead,
    begin,
  }

  return (
    <DeployContext.Provider value={value}>
      {children}
    </DeployContext.Provider>
  )
}

export function DeploymentNotifications() {
  const context = useContext(DeployContext)
  const [open, setOpen] = useState(false)
  const [attention, setAttention] = useState(false)

  useEffect(() => {
    if (!context?.notificationUnread || context.phase === 'running' || !context.completedAt) {
      setAttention(false)
      return
    }
    const remaining = 5 * 60_000 - (Date.now() - context.completedAt)
    if (remaining <= 0) return
    setAttention(true)
    const timer = window.setTimeout(() => setAttention(false), remaining)
    return () => window.clearTimeout(timer)
  }, [context?.completedAt, context?.notificationUnread, context?.phase])

  if (!context) return null
  const title = context.phase === 'running' ? 'Deployment in progress' : context.phase === 'deployed' ? 'Deployment complete' : 'Deployment failed'
  const toggle = () => {
    const next = !open
    setOpen(next)
    if (next) context.markNotificationRead()
  }

  return (
    <div className="notification-center">
      <Button appearance="subtle" className={'notification-trigger' + (attention ? ' attention' : '')} icon={context.notificationUnread ? <AlertFilled /> : <AlertRegular />} aria-label="Notifications" aria-expanded={open} onClick={toggle} />
      {context.notificationUnread && <span className="notification-dot" aria-hidden="true" />}
      {open && (
        <section className="notification-panel" aria-label="Notifications">
          <div className="notification-panel-head">
            <strong>Notifications</strong>
            <button type="button" onClick={() => setOpen(false)} aria-label="Close notifications"><DismissRegular /></button>
          </div>
          {context.phase === 'idle' ? <div className="notification-empty">No notifications yet.</div> : (
            <div className={`notification-item ${context.phase}`} role={context.phase === 'error' ? 'alert' : 'status'}>
              <span className="notification-status" aria-hidden="true" />
              <div>
                <strong>{title}</strong>
                <span>{context.phase === 'running' ? context.message || 'Starting…' : context.result?.message || context.message}</span>
                {context.completedAt && <small>{new Date(context.completedAt).toLocaleString()}</small>}
                <div className="notification-links">
                  {context.portalUrl && <a href={context.portalUrl} target="_blank" rel="noreferrer">Azure portal</a>}
                  {context.phase === 'deployed' && context.result?.url && <a href={context.result.url} target="_blank" rel="noreferrer">Open app</a>}
                </div>
              </div>
            </div>
          )}
        </section>
      )}
    </div>
  )
}

export function useDeployJob(): DeployJobValue {
  const context = useContext(DeployContext)
  const owner = useRef(Symbol('deploy-owner'))
  const begin = context?.begin
  const deploy = useCallback(
    (p: { subscription: string; agent: { fileName: string; content: string }; target: DeployTarget }) =>
      begin ? begin(() => api.startDeploy(p), owner.current) : Promise.reject(new Error('Deploy provider is unavailable.')),
    [begin],
  )
  const redeploy = useCallback(
    (p: { subscription: string; resourceGroup: string; app: string }) =>
      begin ? begin(() => api.startRedeploy(p), owner.current) : Promise.reject(new Error('Deploy provider is unavailable.')),
    [begin],
  )
  if (!context) throw new Error('useDeployJob must be used within DeployProvider.')
  const ownsJob = context.owner === owner.current
  const phase = ownsJob ? context.phase : 'idle'
  return {
    phase,
    result: ownsJob ? context.result : null,
    portalUrl: ownsJob ? context.portalUrl : undefined,
    message: ownsJob ? context.message : '',
    deploy,
    redeploy,
  }
}

function GrantAccess({
  grant,
  principalId,
}: {
  grant: { subscription: string; resourceGroup: string; account: string; tenantId?: string }
  principalId: string
}) {
  const [state, setState] = useState<'idle' | 'granting' | 'done' | 'error'>('idle')
  const [detail, setDetail] = useState('')

  const run = async () => {
    setState('granting')
    setDetail('')
    try {
      const r = await api.grantFoundryAccess({
        subscription: grant.subscription,
        resourceGroup: grant.resourceGroup,
        account: grant.account,
        principalId,
      })
      if (r.granted.length) {
        setState('done')
        setDetail(r.granted.join(', '))
      } else {
        setState('error')
        setDetail(r.failed.map((f) => `${f.role}: ${f.error}`).join('; ') || 'no roles granted')
      }
    } catch (e) {
      setState('error')
      setDetail((e as Error).message)
    }
  }

  const accountId = `/subscriptions/${grant.subscription}/resourceGroups/${grant.resourceGroup}/providers/Microsoft.CognitiveServices/accounts/${grant.account}`
  const iamUrl = `https://portal.azure.com/#${grant.tenantId ? `@${grant.tenantId}` : ''}/resource${accountId}/users`

  return (
    <div style={{ marginTop: 8 }}>
      <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
        The app’s identity needs access to Foundry <span className="mono">{grant.account}</span> to call the
        model.
      </div>
      <Button
        appearance="primary"
        size="small"
        onClick={() => void run()}
        disabled={state === 'granting' || state === 'done'}
      >
        {state === 'granting' ? 'Granting…' : state === 'done' ? '✓ Access granted' : '🔑 Grant access'}
      </Button>{' '}
      <a href={iamUrl} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
        or grant in the portal ↗
      </a>
      {state === 'done' && detail && (
        <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
          Granted: {detail}
        </span>
      )}
      {state === 'error' && (
        <div className="muted" style={{ color: 'var(--red)', fontSize: 12, marginTop: 4 }}>
          Grant failed: {detail}
        </div>
      )}
    </div>
  )
}

export function GitHubConnect({
  github,
  defaultCollapsed = false,
}: {
  github: { subscription: string; resourceGroup: string; app: string }
  defaultCollapsed?: boolean
}) {
  const { subscription, resourceGroup, app } = github
  const [collapsed, setCollapsed] = useState(defaultCollapsed)
  const [status, setStatus] = useState<GitHubStatus | null>(null)
  const [appConn, setAppConn] = useState<GitHubAppConnection | null>(null)
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<'new' | 'existing'>('new')
  const [publishMode, setPublishMode] = useState<GitHubPublishMode>('pr')
  const [repoName, setRepoName] = useState(app)
  const [priv, setPriv] = useState(true)
  const [repos, setRepos] = useState<GitHubRepo[] | null>(null)
  const [existingRepo, setExistingRepo] = useState('')
  const [pushing, setPushing] = useState(false)
  const [result, setResult] = useState<GitHubConnectResult | null>(null)
  const [error, setError] = useState('')
  const [githubSettingsUrl, setGithubSettingsUrl] = useState('')
  const [changingRepo, setChangingRepo] = useState(false)
  const [unlinking, setUnlinking] = useState(false)
  const [changeNote, setChangeNote] = useState('')
  const [provisioning, setProvisioning] = useState(false)
  const [provisionMsg, setProvisionMsg] = useState('')
  const [provisionRuns, setProvisionRuns] = useState('')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const refreshStatus = useCallback(async () => {
    const [s, c] = await Promise.all([
      api.githubStatus().catch(() => ({ configured: false, connected: false }) as GitHubStatus),
      api
        .githubAppConnection({ subscription, resourceGroup, app })
        .catch(() => ({ connected: false }) as GitHubAppConnection),
    ])
    setStatus(s)
    setAppConn(c)
  }, [subscription, resourceGroup, app])

  const handleGitHubError = async (caught: unknown) => {
    setError((caught as Error).message)
    const settingsUrl = caught instanceof ApiError ? caught.data.settingsUrl : ''
    setGithubSettingsUrl(
      typeof settingsUrl === 'string' &&
        /^https:\/\/github\.com\/(?:settings\/installations(?:\/\d+)?|apps\/[A-Za-z0-9-]+\/installations\/new)\/?$/.test(settingsUrl)
        ? settingsUrl
        : '',
    )
    if (caught instanceof ApiError && caught.data.error === 'github_session_expired') {
      setRepos(null)
      await refreshStatus()
    }
  }

  useEffect(() => {
    void refreshStatus()
  }, [refreshStatus])

  // Refresh once the OAuth popup reports back (content is untrusted — we re-check
  // the real connection state via the authenticated status endpoint).
  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      if ((e.data as { type?: string })?.type === 'github-oauth') void refreshStatus()
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [refreshStatus])

  // Stop any in-flight sign-in poll when the component unmounts.
  useEffect(
    () => () => {
      if (pollRef.current) clearInterval(pollRef.current)
    },
    [],
  )

  const connect = async () => {
    setError('')
    setBusy(true)
    const localHost = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
    if (localHost) {
      try {
        await api.githubLocalSession()
        await refreshStatus()
      } catch (e) {
        setError((e as Error).message)
      } finally {
        setBusy(false)
      }
      return
    }
    // Open the popup synchronously (avoids blockers), then point it at GitHub.
    const popup = window.open('', 'github-oauth', 'width=760,height=820')
    let authorizeUrl = ''
    try {
      const resp = await api.githubLoginUrl(`${window.location.origin}/api/github/callback`)
      authorizeUrl = resp.authorizeUrl
    } catch (e) {
      try {
        popup?.close()
      } catch {
        /* ignore */
      }
      setError((e as Error).message)
      setBusy(false)
      return
    }
    if (!popup) {
      setError('The sign-in popup was blocked. Allow pop-ups for this site, then try again.')
      setBusy(false)
      return
    }
    popup.location.href = authorizeUrl

    // Finish by polling the server for the stored connection. This is robust
    // even when the provider page severs the popup's opener link (COOP), which
    // stops the popup's postMessage/self-close from reaching this tab.
    if (pollRef.current) clearInterval(pollRef.current)
    const deadline = Date.now() + 2 * 60 * 1000
    pollRef.current = setInterval(async () => {
      let connected = false
      try {
        connected = (await api.githubStatus()).connected
      } catch {
        /* transient — keep polling */
      }
      if (connected) {
        if (pollRef.current) clearInterval(pollRef.current)
        pollRef.current = null
        setBusy(false)
        try {
          popup.close()
        } catch {
          /* opener link may be severed — the popup shows a “you can close this” note */
        }
        void refreshStatus()
      } else if (Date.now() > deadline) {
        if (pollRef.current) clearInterval(pollRef.current)
        pollRef.current = null
        setBusy(false)
        setError('GitHub sign-in timed out. Please try again.')
      }
    }, 1500)
  }

  const loadRepos = async () => {
    if (repos) return
    try {
      setRepos((await api.githubRepos()).repos)
    } catch (e) {
      await handleGitHubError(e)
    }
  }

  const publish = async (request: {
    mode: 'new' | 'existing'
    publishMode: GitHubPublishMode
    repoName?: string
    private?: boolean
    repo?: string
    branch?: string
  }) => {
    setError('')
    setGithubSettingsUrl('')
    setPushing(true)
    setResult(null)
    try {
      const r = await api.githubConnect({
        subscription,
        resourceGroup,
        app,
        ...request,
      })
      setResult(r)
      setChangingRepo(false)
      await refreshStatus()
    } catch (e) {
      await handleGitHubError(e)
    } finally {
      setPushing(false)
    }
  }

  const createAndPush = async () => {
    const selected = repos?.find((repo) => repo.fullName === existingRepo)
    const branch = mode === 'existing' ? selected?.defaultBranch : 'main'
    if (
      publishMode === 'direct' &&
      !window.confirm(
        `Push the complete deployable source directly to ${branch || 'the default branch'}?\n\n` +
          'This bypasses pull-request review and may start a configured GitHub Actions deployment.',
      )
    ) {
      return
    }
    await publish({
      mode,
      publishMode,
      ...(mode === 'new'
        ? { repoName, private: priv }
        : { repo: existingRepo, ...(branch ? { branch } : {}) }),
    })
  }

  const publishConnected = async (nextMode: GitHubPublishMode) => {
    if (!appConn?.repoUrl) return
    const repo = appConn.repoUrl.replace('https://github.com/', '')
    const branch = appConn.branch || 'main'
    if (
      nextMode === 'direct' &&
      !window.confirm(
        `Push the complete deployable source directly to ${repo}:${branch}?\n\n` +
          'This bypasses pull-request review and may start a configured GitHub Actions deployment.',
      )
    ) {
      return
    }
    setPublishMode(nextMode)
    await publish({ mode: 'existing', publishMode: nextMode, repo, branch })
  }

  // Delete the recorded repo link so the agent can be connected to a different
  // repository, then drop into the New/Existing chooser. Best-effort: even if the
  // clear fails we still let the user pick a new repo (the next connect overwrites
  // the recorded link).
  const changeRepo = async () => {
    setError('')
    setChangeNote('')
    setUnlinking(true)
    try {
      const r = await api.githubUnlink({ subscription, resourceGroup, app })
      if (r.deploymentCenter)
        setChangeNote(
          'The current repository is connected through the Function App’s Deployment Center. ' +
            'Use “Disconnect Deployment Center” to remove that link (or change it in the Azure portal), ' +
            'then connect a new repo below — opening a PR alone won’t repoint the Deployment Center.',
        )
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setUnlinking(false)
    }
    setResult(null)
    setExistingRepo('')
    setChangingRepo(true)
    void refreshStatus()
  }

  // For a repo connected through the Function App's Deployment Center (GitHub
  // Actions set up in the Azure portal), remove that source link too so the app
  // stops pointing at the old repo. Destructive — confirm first.
  const disconnectDeploymentCenter = async () => {
    const ok = window.confirm(
      'Disconnect the Function App from its Deployment Center repository?\n\n' +
        'This removes the GitHub Actions source link on the app so it no longer points at the old repo. ' +
        'The workflow file in the old repo and any federated credentials are left in place. Continue?',
    )
    if (!ok) return
    setError('')
    setChangeNote('')
    setUnlinking(true)
    try {
      const r = await api.githubUnlink({ subscription, resourceGroup, app, deploymentCenter: true })
      setChangeNote(
        r.deploymentCenterCleared
          ? 'Deployment Center disconnected. Connect this agent to a different repository below.'
          : 'Couldn’t remove the Deployment Center link automatically — disconnect it in the Azure portal, then connect a new repo here.',
      )
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setUnlinking(false)
    }
    setResult(null)
    setExistingRepo('')
    setChangingRepo(true)
    void refreshStatus()
  }

  // Provision passwordless GitHub Actions CI/CD (OIDC) from the connected repo to
  // this Function App and re-point the Deployment Center. Infra-mutating — confirm.
  const provisionDeploy = async () => {
    if (!appConn?.repoUrl) return
    const repo = appConn.repoUrl.replace('https://github.com/', '')
    const ok = window.confirm(
      `Set up GitHub Actions deployment from ${repo} to this Function App?\n\n` +
        'This creates a user-assigned managed identity + federated credential, assigns Contributor on ' +
        'the resource group, commits a deploy workflow to the repo, and re-points the Deployment Center. Continue?',
    )
    if (!ok) return
    setError('')
    setProvisionMsg('')
    setProvisionRuns('')
    setProvisioning(true)
    try {
      const r = await api.githubProvisionDeployment({
        subscription,
        resourceGroup,
        app,
        repo,
        branch: appConn.branch || 'main',
      })
      setProvisionMsg(`✓ GitHub Actions configured — pushes to ${appConn.branch || 'main'} now deploy this app.`)
      setProvisionRuns(r.runsUrl)
    } catch (e) {
      await handleGitHubError(e)
    } finally {
      setProvisioning(false)
    }
    void refreshStatus()
  }

  if (!status) return null

  if (!status.configured) {
    return (
      <div className="note" style={{ marginTop: 12 }}>
        🐙 GitHub isn’t configured on the server yet. Set{' '}
        <span className="mono">GITHUB_OAUTH_CLIENT_ID</span> and{' '}
        <span className="mono">GITHUB_OAUTH_CLIENT_SECRET</span>, plus a shared{' '}
        <span className="mono">GITHUB_OAUTH_STATE_SECRET</span>, to connect this agent to a repo.
      </div>
    )
  }

  const disconnect = () => void api.githubDisconnect().then(refreshStatus)
  const repoShort = (appConn?.repoUrl || '').replace('https://github.com/', '')
  const resultIsDirect = result?.publishMode === 'direct'

  return (
    <div className="gh">
      <div className="gh-head">
        <button
          className="gh-collapse"
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? 'Expand GitHub panel' : 'Collapse GitHub panel'}
          aria-label="Toggle GitHub panel"
        >
          {collapsed ? '▸' : '▾'}
        </button>
        <span className="gh-mark">🐙</span>
        <span className="gh-title">GitHub</span>
        {collapsed && (
          <span className="muted" style={{ fontSize: 12, marginLeft: 2 }}>
            {appConn?.connected
              ? `· ${repoShort || 'connected'}`
              : status.connected
                ? `· @${status.login}`
                : '· not connected'}
          </span>
        )}
        <span style={{ flex: 1 }} />
        {status.connected ? (
          <span className="gh-user">
            {status.avatarUrl && <img src={status.avatarUrl} alt="" />}
            @{status.login}
            <Button appearance="subtle" size="small" icon={<DismissRegular />} title="Disconnect GitHub" aria-label="Disconnect GitHub" onClick={disconnect} />
          </span>
        ) : (
          <Button size="small" disabled={busy} onClick={() => void connect()} title="Connect GitHub">
            {busy ? (
              <>
                <span className="gh-spin" /> Connecting…
              </>
            ) : (
              <>🐙 Connect</>
            )}
          </Button>
        )}
      </div>

      {!collapsed && (
        <div className="gh-body">
        {appConn?.connected && status.connected && !result && !changingRepo ? (
          <div className="gh-success">
            <div className="h">✓ Connected to a repository</div>
            <div className="gh-row">
              <a className="btn sm" href={appConn.repoUrl} target="_blank" rel="noreferrer">
                🔗 {repoShort || appConn.repoUrl}
              </a>
              {repoShort && (
                <a
                  className="btn sm"
                  href={`https://vscode.dev/github/${repoShort}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  🧩 Open in VS Code ↗
                </a>
              )}
              {appConn.branch && <span className="badge gray mono">{appConn.branch}</span>}
              {appConn.source === 'deploymentCenter' && <span className="badge blue">Deployment Center</span>}
              {appConn.connectedBy && (
                <span className="muted" style={{ fontSize: 12 }}>
                  connected by @{appConn.connectedBy}
                </span>
              )}
            </div>
            <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
              Publish the complete deployable app source, including saved drafts, through review or directly to the connected branch.
            </div>
            <div className="gh-row" style={{ marginTop: 10 }}>
              <Button appearance="primary" size="small" disabled={pushing} onClick={() => void publishConnected('pr')}>
                {pushing && publishMode === 'pr' ? 'Opening PR…' : 'Create PR'}
              </Button>
              <Button size="small" disabled={pushing} onClick={() => void publishConnected('direct')}>
                {pushing && publishMode === 'direct' ? 'Pushing…' : `Push to ${appConn.branch || 'main'}`}
              </Button>
              <Button size="small" disabled={provisioning} onClick={() => void provisionDeploy()}>
                {provisioning ? (
                  <>
                    <span className="gh-spin" /> Setting up…
                  </>
                ) : (
                  <>⚙️ Set up GitHub Actions deploy</>
                )}
              </Button>
              <Button appearance="subtle" size="small" disabled={unlinking} onClick={() => void changeRepo()}>
                {unlinking ? (
                  <>
                    <span className="gh-spin" /> Updating…
                  </>
                ) : (
                  <>🔁 Change repository</>
                )}
              </Button>
              {appConn.source === 'deploymentCenter' && (
                <button
                  className="btn sm danger"
                  disabled={unlinking}
                  onClick={() => void disconnectDeploymentCenter()}
                >
                  🔌 Disconnect Deployment Center
                </button>
              )}
            </div>
            {provisionMsg && (
              <div className="gh-row" style={{ marginTop: 8 }}>
                <span className="badge green">GitHub Actions</span>
                <span className="muted" style={{ fontSize: 12 }}>
                  {provisionMsg}
                </span>
                {provisionRuns && (
                  <a className="btn sm" href={provisionRuns} target="_blank" rel="noreferrer">
                    View Actions ↗
                  </a>
                )}
              </div>
            )}
          </div>
        ) : result ? (
          <div className="gh-success">
            <div className="h">
              {resultIsDirect
                ? `✓ Source pushed to ${result.branch}`
                : `✓ Pull request opened${result.prNumber ? ` · #${result.prNumber}` : ''}`}
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {resultIsDirect ? 'Updated' : 'Review and merge to update'}{' '}
              <span className="mono">
                {result.owner}/{result.name}
              </span>
              .
            </div>
            <div className="gh-row">
              <a
                className="btn sm primary"
                href={resultIsDirect && result.commitSha
                  ? `${result.htmlUrl}/commit/${result.commitSha}`
                  : result.prUrl || result.htmlUrl}
                target="_blank"
                rel="noreferrer"
              >
                {resultIsDirect ? 'View commit →' : 'View pull request →'}
              </a>
              <a
                className="btn sm"
                href={`https://vscode.dev/github/${result.owner}/${result.name}/tree/${result.branch}`}
                target="_blank"
                rel="noreferrer"
              >
                🧩 Open in VS Code ↗
              </a>
              <span className="badge gray mono">
                {result.branch}
                {!resultIsDirect && result.base ? ` → ${result.base}` : ''}
              </span>
              <Button appearance="subtle" size="small" onClick={() => setResult(null)} title="Back to the repository">
                ← Back
              </Button>
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
              {result.deploymentCenter
                ? '✓ Also recorded in the Function App’s Deployment Center.'
                : 'Recorded on the Function App. Use “⚙️ Set up GitHub Actions deploy” to add it to the Deployment Center (disconnect an existing one first).'}
            </div>
            {!result.stored && (
              <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                Couldn’t save the repo link on the app (permission) — the PR was still opened.
              </div>
            )}
          </div>
        ) : !status.connected ? (
          <div className="gh-cta">
            {appConn?.connected ? (
              <p>
                This agent is linked to{' '}
                <span className="mono">
                  {(appConn.repoUrl || '').replace('https://github.com/', '') || 'a repository'}
                </span>
                . Reconnect your GitHub account to manage it, change the repo, or open a pull request.
              </p>
            ) : (
              <p>
                Open a pull request with this agent’s source — on a new branch named for you. Review &amp;
                merge to publish.
              </p>
            )}
            <Button appearance="primary" disabled={busy} onClick={() => void connect()}>
              {busy ? (
                <>
                  <span className="gh-spin" /> Waiting for GitHub…
                </>
              ) : (
                <>🐙 {appConn?.connected ? 'Reconnect GitHub' : 'Connect GitHub'}</>
              )}
            </Button>
            {busy && (
              <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
                Complete sign-in in the popup — this tab updates automatically.
              </div>
            )}
          </div>
        ) : (
          <>
            {changingRepo && (
              <div className="gh-row" style={{ marginBottom: 8 }}>
                <span className="muted" style={{ fontSize: 12, flex: 1 }}>
                  Connect this agent to a different repository.
                </span>
                {appConn?.connected && (
                  <button
                    type="button"
                    className="btn sm ghost"
                    onClick={() => {
                      setChangingRepo(false)
                      setChangeNote('')
                    }}
                  >
                    Cancel
                  </button>
                )}
              </div>
            )}
            {changeNote && (
              <div className="note" style={{ marginBottom: 8 }}>
                {changeNote}
              </div>
            )}
            <div className="gh-seg">
              <button
                type="button"
                className={`gh-opt${mode === 'new' ? ' active' : ''}`}
                onClick={() => setMode('new')}
              >
                <span className="t">✨ New repository</span>
                <span className="d">Create a deployable repository</span>
              </button>
              <button
                type="button"
                className={`gh-opt${mode === 'existing' ? ' active' : ''}`}
                onClick={() => {
                  setMode('existing')
                  void loadRepos()
                }}
              >
                <span className="t">📁 Existing repository</span>
                <span className="d">Publish into a repository you pick</span>
              </button>
            </div>

            {mode === 'new' ? (
              <>
                <div className="gh-field">
                  <label>Repository name</label>
                  <Input value={repoName} onChange={(_, data) => setRepoName(data.value)} placeholder="my-agent" />
                </div>
                <div className="gh-field">
                  <label>Visibility</label>
                  <div className="gh-vis">
                    <button
                      type="button"
                      className={`gh-opt${priv ? ' active' : ''}`}
                      onClick={() => setPriv(true)}
                    >
                      <span className="t">🔒 Private</span>
                    </button>
                    <button
                      type="button"
                      className={`gh-opt${!priv ? ' active' : ''}`}
                      onClick={() => setPriv(false)}
                    >
                      <span className="t">🌐 Public</span>
                    </button>
                  </div>
                </div>
              </>
            ) : (
              <div className="gh-field">
                <label>Repository</label>
                <SearchableSelect
                  value={existingRepo}
                  onChange={setExistingRepo}
                  options={(repos ?? []).map((r) => ({
                    value: r.fullName,
                    label: r.private ? `${r.fullName} (private)` : r.fullName,
                  }))}
                  placeholder={repos ? 'Select a repository…' : 'Loading your repositories…'}
                  loading={!repos}
                  ariaLabel="Repository"
                />
              </div>
            )}

            <div className="gh-field">
              <label>Publish changes</label>
              <div className="gh-vis" role="group" aria-label="GitHub publication mode">
                <button
                  type="button"
                  className={`gh-opt${publishMode === 'pr' ? ' active' : ''}`}
                  aria-pressed={publishMode === 'pr'}
                  onClick={() => setPublishMode('pr')}
                >
                  <span className="t">Create pull request</span>
                  <span className="d">Review before merging</span>
                </button>
                <button
                  type="button"
                  className={`gh-opt${publishMode === 'direct' ? ' active' : ''}`}
                  aria-pressed={publishMode === 'direct'}
                  onClick={() => setPublishMode('direct')}
                >
                  <span className="t">Push to default branch</span>
                  <span className="d">Publish without review</span>
                </button>
              </div>
            </div>

            <button
              className="btn primary"
              style={{ width: '100%', justifyContent: 'center', marginTop: 4 }}
              disabled={pushing || (mode === 'new' ? !repoName.trim() : !existingRepo)}
              onClick={() => void createAndPush()}
            >
              {pushing ? (
                <>
                  <span className="gh-spin" /> {publishMode === 'direct' ? 'Pushing source…' : 'Opening pull request…'}
                </>
              ) : mode === 'new' && publishMode === 'pr' ? (
                'Create repository & open PR'
              ) : mode === 'new' ? (
                'Create repository & push'
              ) : publishMode === 'direct' ? (
                'Push to default branch'
              ) : (
                'Open pull request'
              )}
            </button>
            {pushing && (
              <>
                <div className="skeleton shimmer-bar" />
                <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                  {mode === 'new'
                    ? `Creating the repository and ${publishMode === 'direct' ? 'pushing source…' : 'opening a pull request…'}`
                    : publishMode === 'direct' ? 'Pushing source…' : 'Opening the pull request…'}
                </div>
              </>
            )}
          </>
        )}

        {error && (
          <div className="gh-err">
            <div>{error}</div>
            {githubSettingsUrl && (
              <a className="btn sm" href={githubSettingsUrl} target="_blank" rel="noreferrer">
                Install or review GitHub App access
              </a>
            )}
          </div>
        )}
        </div>
      )}
    </div>
  )
}

export function DeploymentStatus({
  phase,
  result,
  portalUrl,
  message,
  grant,
  github,
}: {
  phase: DeployPhase
  result: DeployResult | null
  portalUrl?: string
  message?: string
  grant?: { subscription: string; resourceGroup: string; account: string; tenantId?: string }
  github?: { subscription: string; resourceGroup: string; app: string }
}) {
  if (phase === 'idle') return null
  return (
    <div className="note" style={{ marginTop: 12 }}>
      {phase === 'running' && (
        <strong>
          Deploying…{message ? <span className="muted" style={{ fontWeight: 400 }}> · {message}</span> : null}
        </strong>
      )}
      {phase === 'deployed' && <strong>Deployed.</strong>}
      {phase === 'error' && <strong style={{ color: 'var(--red)' }}>Deploy failed.</strong>}{' '}
      {phase !== 'running' && result?.message}
      {portalUrl && (
        <div style={{ marginTop: 6 }}>
          ▶{' '}
          <a href={portalUrl} target="_blank" rel="noreferrer">
            {phase === 'running'
              ? 'View deployment progress in the Azure portal ↗'
              : 'Open in the Azure portal ↗'}
          </a>
        </div>
      )}
      {phase === 'deployed' && result?.url && (
        <div style={{ marginTop: 6 }}>
          App:{' '}
          <a href={result.url} target="_blank" rel="noreferrer">
            {result.url}
          </a>
        </div>
      )}
      {phase === 'deployed' && result?.insightsUrl && (
        <div style={{ marginTop: 6 }}>
          Telemetry:{' '}
          <a href={result.insightsUrl} target="_blank" rel="noreferrer">
            View in Application Insights ↗
          </a>
        </div>
      )}
      {phase === 'deployed' && result?.grantOutcome === 'granted' && (
        <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
          ✓ Granted the app’s identity access to Foundry{grant?.account ? ` (${grant.account})` : ''}. Role
          changes can take a couple of minutes to take effect.
        </div>
      )}
      {phase === 'deployed' && result?.grantOutcome !== 'granted' && result?.principalId && grant?.account && (
        <GrantAccess grant={grant} principalId={result.principalId} />
      )}
      {phase === 'deployed' && github?.app && <GitHubConnect github={github} />}
      {result?.files && result.files.length > 0 && (
        <div style={{ marginTop: 6 }}>
          Source:{' '}
          {result.files.map((f) => (
            <span key={f} className="badge gray mono" style={{ marginRight: 6 }}>
              {f}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
