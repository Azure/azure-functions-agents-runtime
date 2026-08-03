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

function GitHubConnect({ github }: { github: { subscription: string; resourceGroup: string; app: string } }) {
  const [status, setStatus] = useState<GitHubStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<'new' | 'existing'>('new')
  const [repoName, setRepoName] = useState(github.app)
  const [priv, setPriv] = useState(true)
  const [repos, setRepos] = useState<GitHubRepo[] | null>(null)
  const [existingRepo, setExistingRepo] = useState('')
  const [pushing, setPushing] = useState(false)
  const [result, setResult] = useState<GitHubConnectResult | null>(null)
  const [error, setError] = useState('')

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await api.githubStatus())
    } catch {
      setStatus({ configured: false, connected: false })
    }
  }, [])

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

  const connect = async () => {
    setError('')
    setBusy(true)
    // Open the popup synchronously (avoids blockers), then point it at GitHub.
    const popup = window.open('', 'github-oauth', 'width=760,height=820')
    try {
      const { authorizeUrl } = await api.githubLoginUrl()
      if (popup) popup.location.href = authorizeUrl
    } catch (e) {
      if (popup) popup.close()
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
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
        subscription: github.subscription,
        resourceGroup: github.resourceGroup,
        app: github.app,
        mode,
        ...(mode === 'new' ? { repoName, private: priv } : { repo: existingRepo }),
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
      <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>
        🐙 GitHub sign-in isn’t configured on the server yet. Set{' '}
        <span className="mono">GITHUB_OAUTH_CLIENT_ID</span> and{' '}
        <span className="mono">GITHUB_OAUTH_CLIENT_SECRET</span> to connect this agent to a repo.
      </div>
    )
  }

  return (
    <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 10 }}>
      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>🐙 Connect to GitHub</div>
      {!status.connected ? (
        <>
          <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
            Sign in to push this agent’s source to a repo for history and pull-request edits.
          </div>
          <button className="btn sm primary" disabled={busy} onClick={() => void connect()}>
            {busy ? 'Opening…' : '🐙 Connect GitHub'}
          </button>
        </>
      ) : result ? (
        <div style={{ fontSize: 13 }}>
          ✓ Pushed to{' '}
          <a href={result.htmlUrl} target="_blank" rel="noreferrer">
            {result.owner}/{result.name}
          </a>{' '}
          <span className="muted">({result.branch})</span>
          {!result.stored && (
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              Couldn’t save the repo link on the app (permission) — the push still succeeded.
            </div>
          )}
        </div>
      ) : (
        <>
          <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
            Connected as <strong>{status.login}</strong>.
          </div>
          <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
            <button className={`btn sm ${mode === 'new' ? 'primary' : ''}`} onClick={() => setMode('new')}>
              New repo
            </button>
            <button
              className={`btn sm ${mode === 'existing' ? 'primary' : ''}`}
              onClick={() => {
                setMode('existing')
                void loadRepos()
              }}
            >
              Existing repo
            </button>
          </div>
          {mode === 'new' ? (
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <input
                value={repoName}
                onChange={(e) => setRepoName(e.target.value)}
                placeholder="repo-name"
                style={{ minWidth: 200 }}
              />
              <label style={{ fontSize: 12, display: 'flex', gap: 4, alignItems: 'center' }}>
                <input type="checkbox" checked={priv} onChange={(e) => setPriv(e.target.checked)} /> Private
              </label>
            </div>
          ) : (
            <select
              value={existingRepo}
              onChange={(e) => setExistingRepo(e.target.value)}
              style={{ minWidth: 260 }}
            >
              <option value="">{repos ? 'Select a repo…' : 'Loading…'}</option>
              {repos?.map((r) => (
                <option key={r.fullName} value={r.fullName}>
                  {r.fullName}
                  {r.private ? ' (private)' : ''}
                </option>
              ))}
            </select>
          )}
          <div style={{ marginTop: 8 }}>
            <button
              className="btn sm primary"
              disabled={pushing || (mode === 'new' ? !repoName.trim() : !existingRepo)}
              onClick={() => void createAndPush()}
            >
              {pushing ? 'Pushing…' : mode === 'new' ? 'Create & push' : 'Push to repo'}
            </button>{' '}
            <button className="btn sm" onClick={() => void api.githubDisconnect().then(refreshStatus)}>
              Disconnect
            </button>
          </div>
        </>
      )}
      {error && (
        <div className="muted" style={{ color: 'var(--red)', fontSize: 12, marginTop: 6 }}>
          {error}
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
