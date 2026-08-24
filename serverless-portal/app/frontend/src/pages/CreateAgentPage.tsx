import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { useIdentity } from '../identity'
import { CreationSteps, SearchableSelect, Icon } from '../components/ui'
import { Button } from '@coreai/fluentui-react'
import { type Draft, loadDraft, saveDraft, clearDraft, deriveName } from '../agentDraft'

export default function CreateAgentPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const { selected, subscriptions } = useIdentity()
  const [draft, setDraft] = useState<Draft>(loadDraft)
  const [providerExpanded, setProviderExpanded] = useState(true)

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
  const canGenerate =
    !!draft.foundryAccount && !!draft.foundryOpenaiEndpoint && !!draft.description.trim() && !generating
  const generationKey = [foundrySub, draft.foundryAccount, draft.foundryModel, draft.description.trim()].join('|')

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
      const updated: Draft = { ...draft, name, instructions: r.content, generatedFor: generationKey, mdOverride: null }
      setDraft(updated)
      saveDraft(updated)
    } catch (e) {
      setGenError((e as Error).message)
    } finally {
      setGenerating(false)
    }
  }

  const foundryReady = !!draft.foundryModel && !!draft.foundryAccount
  const skillReady =
    !!draft.name.trim() && !!draft.instructions.trim() && draft.generatedFor === generationKey
  const targetReady =
    draft.target === 'existing'
      ? !!draft.existingApp
      : !!draft.newApp.appName && !!draft.newApp.resourceGroup && !!draft.newApp.region
  const step: 1 | 2 = searchParams.get('step') === '2' && foundryReady ? 2 : 1

  const navigateToStep = (nextStep: number) => {
    if (nextStep === 1) setSearchParams({ step: '1' })
    else if (nextStep === 2 && foundryReady) setSearchParams({ step: '2' })
    else if (nextStep === 3 && skillReady) navigate('/new-app/draft')
    else if (nextStep === 4 && skillReady && targetReady) navigate('/new-app/draft?step=4')
  }

  const cancel = () => {
    clearDraft()
    navigate(`/agents/${selected}`)
  }

  return (
    <>
      <div className="breadcrumb">
        Home / <Link to={`/agents/${selected}`}>Hosted Skills</Link> / Create
      </div>
      <div className="create-flow">
        <div className="create-flow-header">
          <h1>Create a New Skill</h1>
          <p>Set up the app and its first Hosted Skill. You can change everything except the app name later.</p>
        </div>

        <CreationSteps
          current={step}
          completed={[foundryReady, skillReady, skillReady && targetReady, false]}
          available={[true, foundryReady, skillReady, skillReady && targetReady]}
          onNavigate={navigateToStep}
          disabled={generating}
        />

      {step === 1 && (
        <>
          <div className="card create-flow-card">
            {providerExpanded ? (
              <>
                <h3>Choose a model provider</h3>
                <p className="muted" style={{ marginTop: 0 }}>
                  Microsoft Foundry is available for this preview.
                </p>
                <div className="model-provider-list" role="radiogroup" aria-label="Model provider">
                  <label className="model-provider-row selected" onClick={() => setProviderExpanded(false)}>
                    <input
                      type="radio"
                      name="model-provider"
                      value="foundry"
                      checked={draft.provider === 'foundry'}
                      onChange={() => {
                        set('provider', 'foundry')
                        setProviderExpanded(false)
                      }}
                    />
                    <span className="model-provider-copy">
                      <strong>Microsoft Foundry</strong>
                      <span>Use a governed model deployment with managed identity.</span>
                    </span>
                    <span className="badge green">Available</span>
                  </label>
                  <label className="model-provider-row disabled" aria-disabled="true">
                    <input type="radio" name="model-provider" value="azure-openai" disabled />
                    <span className="model-provider-copy">
                      <strong>Azure OpenAI</strong>
                      <span>Connect an Azure OpenAI resource and deployment.</span>
                    </span>
                    <span className="badge gray">Coming soon</span>
                  </label>
                  <label className="model-provider-row disabled" aria-disabled="true">
                    <input type="radio" name="model-provider" value="openai" disabled />
                    <span className="model-provider-copy">
                      <strong>OpenAI</strong>
                      <span>Connect with an API key stored in app settings.</span>
                    </span>
                    <span className="badge gray">Coming soon</span>
                  </label>
                </div>
              </>
            ) : (
              <div className="model-provider-summary">
                <div>
                  <span className="model-provider-summary-label">Model provider</span>
                  <strong>Microsoft Foundry</strong>
                  <span>Governed deployment with managed identity</span>
                </div>
                <span className="badge green">Selected</span>
                <Button appearance="subtle" size="small" onClick={() => setProviderExpanded(true)}>Change</Button>
              </div>
            )}
          </div>

          <div className="card create-flow-card model-configuration-card">
            <h3>Configure Microsoft Foundry</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              Select the resource, project, and model deployment that will generate and run this skill.
            </p>
            <div className="model-provider-fields">
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
                <div className="field foundry-resource-field">
                  <label>Foundry resource</label>
                  <div className="foundry-resource-control">
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
                    <Button
                      appearance="subtle"
                      size="small"
                      icon={<Icon name="refresh" size={14} />}
                      onClick={() => void refetchFoundry()}
                      title="Refresh Foundry list"
                      aria-label="Refresh Foundry list"
                    />
                  </div>
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
            </div>
          </div>

          <div className="toolbar" style={{ marginTop: 16 }}>
            <Button appearance="primary" disabled={!foundryReady} onClick={() => navigateToStep(2)}>
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
            <button className="btn sm" onClick={() => navigateToStep(1)} disabled={generating}>
              ← Model
            </button>
            <span className="badge blue">
              <span className="dot" /> {draft.foundryModel || 'no model'}
            </span>
            {draft.foundryMode === 'pick' && draft.foundryAccount && (
              <span className="muted" style={{ fontSize: 12 }}>{draft.foundryAccount}</span>
            )}
          </div>

          <div className={'card create-flow-card generate-skill-card' + (generating ? ' is-generating' : '')} aria-busy={generating}>
            {generating && <div className="generate-shimmer" aria-hidden="true" />}
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
              disabled={generating}
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
              <button className="btn" onClick={cancel} disabled={generating}>
                Cancel
              </button>
              {generating && <span className="generate-status" role="status">Generating skill with {draft.foundryModel}…</span>}
              {!draft.description.trim() && (
                <span className="muted" style={{ fontSize: 12 }}>Describe the agent to generate.</span>
              )}
            </div>
            {genError && (
              <p className="muted" style={{ color: 'var(--red)', fontSize: 12, margin: '10px 0 0' }}>
                Generation failed: {genError}
              </p>
            )}
            {skillReady && (
              <>
                <div className="field" style={{ marginTop: 18 }}>
                  <label>Generated prompt</label>
                  <textarea
                    className="editor"
                    style={{ minHeight: 260 }}
                    spellCheck={false}
                    disabled={generating}
                    value={draft.instructions}
                    onChange={(e) => set('instructions', e.target.value)}
                    aria-label="Generated skill prompt"
                  />
                  <div className="hint">Review and edit this prompt before choosing where the skill will run.</div>
                </div>
                <Button appearance="primary" disabled={generating} onClick={() => navigateToStep(3)}>
                  Continue to deployment target →
                </Button>
              </>
            )}
          </div>
        </>
      )}
      </div>
    </>
  )
}
