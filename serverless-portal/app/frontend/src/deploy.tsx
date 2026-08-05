// Shared deployment job runner + status UI for the Create Agent and Agent
// Detail pages. Starting a deploy returns immediately with a job id and an
// Azure portal link, so the user can watch progress in the portal instead of
// waiting; the hook also polls the job to a terminal state in the background.

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api,
  type DeployResult,
  type DeployTarget,
  type GitHubStatus,
  type GitHubRepo,
  type GitHubConnectResult,
  type GitHubAppConnection,
} from './api'

export type DeployPhase = 'idle' | 'running' | 'deployed' | 'error'

export function useDeployJob() {
  const [phase, setPhase] = useState<DeployPhase>('idle')
  const [result, setResult] = useState<DeployResult | null>(null)
  const [portalUrl, setPortalUrl] = useState<string | undefined>(undefined)
  const [message, setMessage] = useState<string>('')
  const activeJob = useRef<string | null>(null)

  const poll = useCallback(async (jobId: string) => {
    const deadline = Date.now() + 15 * 60 * 1000
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 4000))
      if (activeJob.current !== jobId) return // superseded by a newer deploy
      let state: DeployResult
      try {
        state = await api.getDeployStatus(jobId)
      } catch {
        continue // transient poll error — keep trying
      }
      if (state.portalUrl) setPortalUrl(state.portalUrl)
      setMessage(state.message ?? '')
      if (state.status !== 'running') {
        setResult(state)
        setPhase(state.status === 'deployed' ? 'deployed' : 'error')
        return
      }
    }
    if (activeJob.current === jobId) {
      setPhase('error')
      setResult({ status: 'error', message: 'Deploy timed out. Check the Azure portal.', files: [] })
    }
  }, [])

  const begin = useCallback(
    async (start: () => Promise<{ jobId: string; portalUrl?: string }>) => {
      setPhase('running')
      setResult(null)
      setPortalUrl(undefined)
      setMessage('Starting…')
      try {
        const started = await start()
        activeJob.current = started.jobId
        if (started.portalUrl) setPortalUrl(started.portalUrl)
        void poll(started.jobId)
      } catch (e) {
        activeJob.current = null
        setPhase('error')
        setResult({ status: 'error', message: (e as Error).message, files: [] })
      }
    },
    [poll],
  )

  const deploy = useCallback(
    (p: { subscription: string; agent: { fileName: string; content: string }; target: DeployTarget }) =>
      begin(() => api.startDeploy(p)),
    [begin],
  )

  const redeploy = useCallback(
    (p: { subscription: string; resourceGroup: string; app: string }) => begin(() => api.startRedeploy(p)),
    [begin],
  )

  return { phase, result, portalUrl, message, deploy, redeploy }
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
      <button
        className="btn sm primary"
        onClick={() => void run()}
        disabled={state === 'granting' || state === 'done'}
      >
        {state === 'granting' ? 'Granting…' : state === 'done' ? '✓ Access granted' : '🔑 Grant access'}
      </button>{' '}
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

export function GitHubConnect({ github }: { github: { subscription: string; resourceGroup: string; app: string } }) {
  const { subscription, resourceGroup, app } = github
  const [status, setStatus] = useState<GitHubStatus | null>(null)
  const [appConn, setAppConn] = useState<GitHubAppConnection | null>(null)
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<'new' | 'existing'>('new')
  const [repoName, setRepoName] = useState(app)
  const [priv, setPriv] = useState(true)
  const [repos, setRepos] = useState<GitHubRepo[] | null>(null)
  const [existingRepo, setExistingRepo] = useState('')
  const [pushing, setPushing] = useState(false)
  const [result, setResult] = useState<GitHubConnectResult | null>(null)
  const [error, setError] = useState('')
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
    // Open the popup synchronously (avoids blockers), then point it at GitHub.
    const popup = window.open('', 'github-oauth', 'width=760,height=820')
    let authorizeUrl = ''
    try {
      const resp = await api.githubLoginUrl()
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
      setError((e as Error).message)
    }
  }

  const createAndPush = async () => {
    setError('')
    setPushing(true)
    setResult(null)
    try {
      const r = await api.githubConnect({
        subscription,
        resourceGroup,
        app,
        mode,
        ...(mode === 'new' ? { repoName, private: priv } : { repo: existingRepo }),
      })
      setResult(r)
      setChangingRepo(false)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setPushing(false)
    }
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
      setError((e as Error).message)
    } finally {
      setProvisioning(false)
    }
    void refreshStatus()
  }

  // Push the app's current saved edits (including unpublished drafts) to the
  // connected repo, opening or updating the rolling pull request.
  const pushChanges = async () => {
    if (!appConn?.repoUrl) return
    const repo = appConn.repoUrl.replace('https://github.com/', '')
    setError('')
    setResult(null)
    setPushing(true)
    try {
      const r = await api.githubConnect({
        subscription,
        resourceGroup,
        app,
        mode: 'existing',
        repo,
        branch: appConn.branch || 'main',
      })
      setResult(r)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setPushing(false)
    }
  }

  if (!status) return null

  if (!status.configured) {
    return (
      <div className="note" style={{ marginTop: 12 }}>
        🐙 GitHub isn’t configured on the server yet. Set{' '}
        <span className="mono">GITHUB_OAUTH_CLIENT_ID</span> and{' '}
        <span className="mono">GITHUB_OAUTH_CLIENT_SECRET</span> to connect this agent to a repo.
      </div>
    )
  }

  const disconnect = () => void api.githubDisconnect().then(refreshStatus)
  const repoShort = (appConn?.repoUrl || '').replace('https://github.com/', '')

  return (
    <div className="gh">
      <div className="gh-head">
        <span className="gh-mark">🐙</span>
        <span className="gh-title">GitHub</span>
        <span style={{ flex: 1 }} />
        {status.connected && (
          <span className="gh-user">
            {status.avatarUrl && <img src={status.avatarUrl} alt="" />}
            @{status.login}
            <button className="btn ghost sm" title="Disconnect GitHub" onClick={disconnect}>
              ✕
            </button>
          </span>
        )}
      </div>

      <div className="gh-body">
        {appConn?.connected && !result && !changingRepo ? (
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
              Use “Push changes &amp; open PR” to commit your latest saved edits (including unpublished
              drafts) into a pull request on this repo.
            </div>
            <div className="gh-row" style={{ marginTop: 10 }}>
              <button className="btn sm primary" disabled={pushing} onClick={() => void pushChanges()}>
                {pushing ? (
                  <>
                    <span className="gh-spin" /> Opening pull request…
                  </>
                ) : (
                  <>📤 Push changes &amp; open PR</>
                )}
              </button>
              <button className="btn sm" disabled={provisioning} onClick={() => void provisionDeploy()}>
                {provisioning ? (
                  <>
                    <span className="gh-spin" /> Setting up…
                  </>
                ) : (
                  <>⚙️ Set up GitHub Actions deploy</>
                )}
              </button>
              <button className="btn sm ghost" disabled={unlinking} onClick={() => void changeRepo()}>
                {unlinking ? (
                  <>
                    <span className="gh-spin" /> Updating…
                  </>
                ) : (
                  <>🔁 Change repository</>
                )}
              </button>
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
            <div className="h">✓ Pull request opened{result.prNumber ? ` · #${result.prNumber}` : ''}</div>
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              Review &amp; merge to update{' '}
              <span className="mono">
                {result.owner}/{result.name}
              </span>
              .
            </div>
            <div className="gh-row">
              <a
                className="btn sm primary"
                href={result.prUrl || result.htmlUrl}
                target="_blank"
                rel="noreferrer"
              >
                View pull request →
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
                {result.base ? ` → ${result.base}` : ''}
              </span>
              <button className="btn sm ghost" onClick={() => setResult(null)} title="Back to the repository">
                ← Back
              </button>
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
            <p>
              Open a pull request with this agent’s source — on a new branch named for you. Review &amp; merge
              to publish.
            </p>
            <button className="btn primary" disabled={busy} onClick={() => void connect()}>
              {busy ? (
                <>
                  <span className="gh-spin" /> Waiting for GitHub…
                </>
              ) : (
                <>🐙 Connect GitHub</>
              )}
            </button>
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
                <span className="d">Create a repo &amp; open a PR</span>
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
                <span className="d">Open a PR into a repo you pick</span>
              </button>
            </div>

            {mode === 'new' ? (
              <>
                <div className="gh-field">
                  <label>Repository name</label>
                  <input value={repoName} onChange={(e) => setRepoName(e.target.value)} placeholder="my-agent" />
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
                <select value={existingRepo} onChange={(e) => setExistingRepo(e.target.value)}>
                  <option value="">{repos ? 'Select a repository…' : 'Loading your repositories…'}</option>
                  {repos?.map((r) => (
                    <option key={r.fullName} value={r.fullName}>
                      {r.fullName}
                      {r.private ? ' (private)' : ''}
                    </option>
                  ))}
                </select>
              </div>
            )}

            <button
              className="btn primary"
              style={{ width: '100%', justifyContent: 'center', marginTop: 4 }}
              disabled={pushing || (mode === 'new' ? !repoName.trim() : !existingRepo)}
              onClick={() => void createAndPush()}
            >
              {pushing ? (
                <>
                  <span className="gh-spin" /> Opening pull request…
                </>
              ) : mode === 'new' ? (
                'Create repository & open PR'
              ) : (
                'Open pull request'
              )}
            </button>
          </>
        )}

        {error && <div className="gh-err">{error}</div>}
      </div>
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
