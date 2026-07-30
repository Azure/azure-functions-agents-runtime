// Shared deployment job runner + status UI for the Create Agent and Agent
// Detail pages. Starting a deploy returns immediately with a job id and an
// Azure portal link, so the user can watch progress in the portal instead of
// waiting; the hook also polls the job to a terminal state in the background.

import { useCallback, useRef, useState } from 'react'
import { api, type DeployResult, type DeployTarget } from './api'

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

export function DeploymentStatus({
  phase,
  result,
  portalUrl,
  message,
}: {
  phase: DeployPhase
  result: DeployResult | null
  portalUrl?: string
  message?: string
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
