import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { useIdentity } from '../identity'
import { Callout } from '../components/ui'
import { type Draft, loadDraft, saveDraft, clearDraft, deriveName } from '../agentDraft'

export default function CreateAgentPage() {
  const navigate = useNavigate()
  const { selected, subscriptions } = useIdentity()
  const [draft, setDraft] = useState<Draft>(loadDraft)

  // Persist to sessionStorage on every change (auto-save for the session).
  useEffect(() => {
    saveDraft(draft)
  }, [draft])

  const foundrySub = draft.foundrySubscription || selected

  const {
    data: foundryData,
    isFetching: foundryLoading,
    refetch: refetchFoundry,
  } = useQuery({
    queryKey: ['foundry', foundrySub],
    queryFn: () => api.listFoundry(foundrySub),
    enabled: !!foundrySub,
    staleTime: 5 * 60 * 1000,
  })
  const foundryAccounts = foundryData?.accounts ?? []
  const selectedAccount = foundryAccounts.find((a) => a.name === draft.foundryAccount)

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }))

  // Switch the Foundry subscription → clear the picked account/model.
  const selectFoundrySub = (sub: string) =>
    setDraft((d) => ({
      ...d,
      foundrySubscription: sub,
      foundryAccount: '',
      foundryResourceGroup: '',
      foundryOpenaiEndpoint: '',
      foundryEndpoint: '',
      foundryModel: '',
    }))

  // Pick a Foundry account → seed its rg/endpoint, and auto-select a lone project/model.
  const selectAccount = (name: string) => {
    const acc = foundryAccounts.find((a) => a.name === name)
    setDraft((d) => ({
      ...d,
      foundryAccount: name,
      foundryResourceGroup: acc?.resourceGroup ?? '',
      foundryOpenaiEndpoint: acc?.openaiEndpoint ?? '',
      foundryEndpoint: acc && acc.projects.length === 1 ? acc.projects[0].endpoint : '',
      foundryModel: acc && acc.models.length === 1 ? acc.models[0].deployment : '',
    }))
  }

  // Manual entry clears the picker-derived account fields (which the AI generator
  // needs), so ✨ Generate is only offered when a model is actually selected.
  const setFoundryMode = (mode: 'pick' | 'manual') =>
    setDraft((d) =>
      mode === 'manual'
        ? { ...d, foundryMode: mode, foundryAccount: '', foundryResourceGroup: '', foundryOpenaiEndpoint: '' }
        : { ...d, foundryMode: mode },
    )

  const [generating, setGenerating] = useState(false)
  const [genError, setGenError] = useState<string | null>(null)
  const [step, setStep] = useState<1 | 2>(draft.foundryModel ? 2 : 1)
  const canGenerate =
    !!draft.foundryAccount && !!draft.foundryOpenaiEndpoint && !!draft.description.trim() && !generating

  // Generate the agent's instructions, then open the generated app to review,
  // deploy, and connect GitHub. Nothing is deployed on this page.
  const generateAndOpen = async () => {
    if (!canGenerate) return
    setGenerating(true)
    setGenError(null)
    try {
      const name = draft.name.trim() || deriveName(draft.description)
      const r = await api.generateAgentMd({
        subscription: foundrySub,
        name,
        description: draft.description,
        foundry: {
          resourceGroup: draft.foundryResourceGroup,
          account: draft.foundryAccount,
          openaiEndpoint: draft.foundryOpenaiEndpoint,
          model: draft.foundryModel,
        },
      })
      const updated: Draft = { ...draft, name, instructions: r.content, mdOverride: null }
      setDraft(updated)
      saveDraft(updated)
      navigate('/new-app/draft')
    } catch (e) {
      setGenError((e as Error).message)
    } finally {
      setGenerating(false)
    }
  }

  const foundryReady = !!draft.foundryModel && (draft.foundryMode === 'pick' || !!draft.foundryEndpoint)

  const cancel = () => {
    clearDraft()
    navigate(`/agents/${selected}`)
  }

  return (
    <>
      <div className="breadcrumb">
        Home / <Link to={`/agents/${selected}`}>AI Apps</Link> / Create
      </div>
      <div className="page-title">
        <h1>Create AI App</h1>
        <span className="badge gray">draft saved in this session</span>
      </div>
      <p className="page-sub">
        Pick a Foundry model and describe the agent — we’ll generate its code. You’ll review, deploy, and
        connect GitHub on the next step. This draft is kept only for this browser session.
      </p>

      <Callout title="What makes this an AI App">
        <div className="muted" style={{ fontSize: 13, maxWidth: 720 }}>
          Deploying sets the <code>AZURE_FUNCTIONS_AGENTS_PROVIDER</code> app setting on the Function App — the
          marker the portal uses to discover it as an AI App.
        </div>
      </Callout>

      <div className="steps">
        <span className={'step' + (step === 1 ? ' active' : ' done')}>1 · Model</span>
        <span className="step-sep">→</span>
        <span className={'step' + (step === 2 ? ' active' : '')}>2 · Describe &amp; generate</span>
      </div>

      {step === 1 && (
        <>
          <div className="card">
            <h3>Every agent needs a Foundry model</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              Pick a deployed model from any subscription — or enter its details — to continue. The model runs
              your agent and powers ✨ Generate.
            </p>
            <div style={{ display: 'flex', gap: 16, marginBottom: 4 }}>
              <label className="check" style={{ marginBottom: 0 }}>
                <input
                  type="radio"
                  name="fmode"
                  checked={draft.foundryMode === 'pick'}
                  onChange={() => setFoundryMode('pick')}
                />{' '}
                Select a deployed model
              </label>
              <label className="check" style={{ marginBottom: 0 }}>
                <input
                  type="radio"
                  name="fmode"
                  checked={draft.foundryMode === 'manual'}
                  onChange={() => setFoundryMode('manual')}
                />{' '}
                Enter manually
              </label>
            </div>

            {draft.foundryMode === 'pick' ? (
              <>
                <div className="field" style={{ marginBottom: 8 }}>
                  <label>Subscription</label>
                  <select value={foundrySub} onChange={(e) => selectFoundrySub(e.target.value)}>
                    {subscriptions.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name}
                      </option>
                    ))}
                  </select>
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>
                  <select
                    value={draft.foundryAccount}
                    onChange={(e) => selectAccount(e.target.value)}
                    style={{ flex: 1 }}
                  >
                    <option value="">
                      {foundryLoading
                        ? 'Loading Foundry resources…'
                        : foundryAccounts.length
                          ? 'Select a Foundry resource…'
                          : 'No Foundry resources found'}
                    </option>
                    {foundryAccounts.map((a) => (
                      <option key={a.name} value={a.name}>
                        {a.name} · {a.location}
                      </option>
                    ))}
                  </select>
                  <button className="btn sm" onClick={() => void refetchFoundry()} title="Refresh Foundry list">
                    ↻
                  </button>
                </div>

                {selectedAccount && (
                  <div className="grid cols-2" style={{ gap: 12 }}>
                    {selectedAccount.projects.length > 0 && (
                      <div className="field" style={{ marginBottom: 0 }}>
                        <label>Project</label>
                        <select value={draft.foundryEndpoint} onChange={(e) => set('foundryEndpoint', e.target.value)}>
                          <option value="">Select a project…</option>
                          {selectedAccount.projects.map((p) => (
                            <option key={p.name} value={p.endpoint}>
                              {p.name}
                            </option>
                          ))}
                        </select>
                      </div>
                    )}
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Model deployment</label>
                      <select value={draft.foundryModel} onChange={(e) => set('foundryModel', e.target.value)}>
                        <option value="">
                          {selectedAccount.models.length ? 'Select a model…' : 'No chat models deployed'}
                        </option>
                        {selectedAccount.models.map((m) => (
                          <option key={m.deployment} value={m.deployment}>
                            {m.deployment} ({m.model})
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                )}
                <div className="hint" style={{ marginTop: 8 }}>
                  No model yet?{' '}
                  <a href="https://ai.azure.com" target="_blank" rel="noreferrer">
                    Create one in Azure AI Foundry ↗
                  </a>
                  , then ↻ Refresh. A selected model powers ✨ Generate.
                </div>
              </>
            ) : (
              <div className="grid cols-2" style={{ gap: 12, marginTop: 8 }}>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Model / deployment name</label>
                  <input
                    type="text"
                    value={draft.foundryModel}
                    placeholder="gpt-4o"
                    onChange={(e) => set('foundryModel', e.target.value)}
                  />
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Foundry project endpoint</label>
                  <input
                    type="url"
                    value={draft.foundryEndpoint}
                    placeholder="https://<account>.services.ai.azure.com/api/projects/<project>"
                    onChange={(e) => set('foundryEndpoint', e.target.value)}
                  />
                </div>
                <div className="hint" style={{ gridColumn: '1 / -1' }}>
                  Manual entry — you’ll write the instructions yourself (✨ Generate needs a selected model).
                </div>
              </div>
            )}
          </div>
          <div className="toolbar" style={{ marginTop: 16 }}>
            <button className="btn primary" disabled={!foundryReady} onClick={() => setStep(2)}>
              Continue →
            </button>
            <button className="btn" onClick={cancel}>
              Cancel
            </button>
            {!foundryReady && (
              <span className="muted" style={{ fontSize: 12 }}>
                Select or enter a Foundry model to continue.
              </span>
            )}
          </div>
        </>
      )}

      {step === 2 && (
        <>
          <div className="toolbar" style={{ marginBottom: 8 }}>
            <button className="btn sm" onClick={() => setStep(1)}>
              ← Model
            </button>
            <span className="badge blue">
              <span className="dot" /> {draft.foundryModel || 'no model'}
            </span>
            {draft.foundryMode === 'pick' && draft.foundryAccount && (
              <span className="muted" style={{ fontSize: 12 }}>{draft.foundryAccount}</span>
            )}
          </div>

          <div className="card" style={{ maxWidth: 760 }}>
            <h3>✨ Describe your agent</h3>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              Say what the agent should do in a sentence or two. We’ll generate its{' '}
              <span className="mono">.agent.md</span> with <span className="mono">{draft.foundryModel}</span>.
              Nothing is deployed yet — you’ll review the generated app next, then deploy it or connect GitHub.
            </p>
            <textarea
              className="editor"
              style={{ minHeight: 150 }}
              spellCheck={false}
              placeholder="e.g. Triage inbound support tickets: classify urgency, summarize the issue, and draft a concise reply."
              value={draft.description}
              onChange={(e) => set('description', e.target.value)}
              aria-label="Describe your agent"
            />
            <div className="toolbar" style={{ marginTop: 12 }}>
              <button className="btn primary" onClick={generateAndOpen} disabled={!canGenerate}>
                {generating ? '✨ Generating…' : '✨ Generate app'}
              </button>
              <button className="btn" onClick={cancel}>
                Cancel
              </button>
              {draft.foundryMode === 'manual' && (
                <span className="muted" style={{ fontSize: 12 }}>
                  Generation needs a picked model (← Model), not manual entry.
                </span>
              )}
              {draft.foundryMode === 'pick' && !draft.description.trim() && (
                <span className="muted" style={{ fontSize: 12 }}>Describe the agent to generate.</span>
              )}
            </div>
            {genError && (
              <p className="muted" style={{ color: 'var(--red)', fontSize: 12, margin: '10px 0 0' }}>
                Generation failed: {genError}
              </p>
            )}
          </div>
        </>
      )}
    </>
  )
}
