import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { useIdentity } from '../identity'
import { SearchableSelect, Icon } from '../components/ui'
import { Button } from '@coreai/fluentui-react'
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

  const subOptions = useMemo(
    () => subscriptions.map((s) => ({ value: s.id, label: s.name })),
    [subscriptions],
  )
  const accountOptions = useMemo(
    () => foundryAccounts.map((a) => ({ value: a.name, label: a.name, sublabel: a.location })),
    [foundryAccounts],
  )
  const projectOptions = useMemo(
    () =>
      selectedAccount?.projects.map((p) => ({ value: p.endpoint, label: p.name })) ?? [],
    [selectedAccount],
  )
  const modelOptions = useMemo(
    () =>
      selectedAccount?.models.map((m) => ({
        value: m.deployment,
        label: m.deployment,
        sublabel: m.model,
      })) ?? [],
    [selectedAccount],
  )

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

  const [generating, setGenerating] = useState(false)
  const [genError, setGenError] = useState<string | null>(null)
  const [step, setStep] = useState<1 | 2>(draft.foundryModel ? 2 : 1)
  const [generated, setGenerated] = useState(!!draft.name && !!draft.description)
  const canGenerate =
    !!draft.foundryAccount && !!draft.foundryOpenaiEndpoint && !!draft.description.trim() && !generating

  const generateSkill = async () => {
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
      setGenerated(true)
    } catch (e) {
      setGenError((e as Error).message)
    } finally {
      setGenerating(false)
    }
  }

  const foundryReady = !!draft.foundryModel && !!draft.foundryAccount

  const cancel = () => {
    clearDraft()
    navigate(`/agents/${selected}`)
  }

  return (
    <>
      <div className="breadcrumb">
        Home / <Link to={`/agents/${selected}`}>Hosted Skills</Link> / Create
      </div>
      <div className="page-title">
        <h1>New Skill</h1>
      </div>

      <div className="steps">
        <span className={'step' + (step === 1 ? ' active' : ' done')}>1 · Model</span>
        <span className="step-sep">→</span>
        <span className={'step' + (step === 2 ? ' active' : '')}>2 · Generate skill</span>
        <span className="step-sep">→</span>
        <span className="step">3 · Deployment target</span>
        <span className="step-sep">→</span>
        <span className="step">4 · Review and deploy</span>
      </div>

      {step === 1 && (
        <>
          <div className="card">
            <h3>Choose a Microsoft Foundry model</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              Select the subscription, Foundry resource, project, and deployed model that will generate and run this skill.
            </p>
            <>
                <div className="field" style={{ marginBottom: 8 }}>
                  <label>Subscription</label>
                  <SearchableSelect
                    value={foundrySub}
                    onChange={selectFoundrySub}
                    options={subOptions}
                    placeholder="Select a subscription…"
                    ariaLabel="Foundry subscription"
                  />
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>
                  <div style={{ flex: 1 }}>
                    <SearchableSelect
                      value={draft.foundryAccount}
                      onChange={selectAccount}
                      options={accountOptions}
                      placeholder={
                        foundryAccounts.length ? 'Select a Foundry resource…' : 'No Foundry resources found'
                      }
                      loading={foundryLoading}
                      ariaLabel="Foundry resource"
                    />
                  </div>
                  <Button
                    appearance="subtle"
                    size="small"
                    icon={<Icon name="refresh" size={14} />}
                    onClick={() => void refetchFoundry()}
                    title="Refresh Foundry list"
                    aria-label="Refresh Foundry list"
                  />
                </div>

                {selectedAccount && (
                  <div className="grid cols-2" style={{ gap: 12 }}>
                    {selectedAccount.projects.length > 0 && (
                      <div className="field" style={{ marginBottom: 0 }}>
                        <label>Project</label>
                        <SearchableSelect
                          value={draft.foundryEndpoint}
                          onChange={(v) => set('foundryEndpoint', v)}
                          options={projectOptions}
                          placeholder="Select a project…"
                          ariaLabel="Foundry project"
                        />
                      </div>
                    )}
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Model deployment</label>
                      <SearchableSelect
                        value={draft.foundryModel}
                        onChange={(v) => set('foundryModel', v)}
                        options={modelOptions}
                        placeholder={selectedAccount.models.length ? 'Select a model…' : 'No chat models deployed'}
                        ariaLabel="Model deployment"
                      />
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
          </div>

          <div className="toolbar" style={{ marginTop: 16 }}>
            <Button appearance="primary" disabled={!foundryReady} onClick={() => setStep(2)}>
              Continue to generate →
            </Button>
            <Button onClick={cancel}>Cancel</Button>
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
            <h3>
              <Icon name="sparkles" size={15} style={{ verticalAlign: '-2px' }} /> Generate your skill
            </h3>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              Describe the outcome. <span className="mono">{draft.foundryModel}</span> will generate the skill prompt for you to review and edit.
            </p>
            <textarea
              className="editor"
              style={{ minHeight: 150 }}
              spellCheck={false}
              placeholder="e.g. Triage inbound support tickets: classify urgency, summarize the issue, and draft a concise reply."
              value={draft.description}
              onChange={(e) => set('description', e.target.value)}
              aria-label="Describe your skill"
            />
            <div className="toolbar" style={{ marginTop: 12 }}>
              <button className="btn primary" onClick={generateSkill} disabled={!canGenerate}>
                {generating ? (
                  'Generating…'
                ) : (
                  <>
                    <Icon name="sparkles" size={14} /> Generate skill
                  </>
                )}
              </button>
              <button className="btn" onClick={cancel}>
                Cancel
              </button>
              {!draft.description.trim() && (
                <span className="muted" style={{ fontSize: 12 }}>Describe the agent to generate.</span>
              )}
            </div>
            {genError && (
              <p className="muted" style={{ color: 'var(--red)', fontSize: 12, margin: '10px 0 0' }}>
                Generation failed: {genError}
              </p>
            )}
            {generated && (
              <>
                <div className="field" style={{ marginTop: 18 }}>
                  <label>Generated prompt</label>
                  <textarea
                    className="editor"
                    style={{ minHeight: 260 }}
                    spellCheck={false}
                    value={draft.instructions}
                    onChange={(e) => set('instructions', e.target.value)}
                    aria-label="Generated skill prompt"
                  />
                  <div className="hint">Review and edit this prompt before choosing where the skill will run.</div>
                </div>
                <Button appearance="primary" onClick={() => navigate('/new-app/draft')}>
                  Continue to deployment target →
                </Button>
              </>
            )}
          </div>
        </>
      )}
    </>
  )
}
