// Reusable "where does this deploy?" chooser: add to an existing Function App,
// or create a new Flex Consumption one. Controlled + prop-driven so the create
// flow and any future redeploy flow can share it. Emits partial patches.

export interface ExistingApp {
  name: string
  resourceGroup: string
}

export interface ResourceGroupOption {
  name: string
  location: string
}

export interface NewAppTarget {
  appName: string
  region: string
  rgMode: 'existing' | 'new'
  resourceGroup: string
}

export interface DeployTargetValue {
  mode: 'existing' | 'new'
  existingApp: string
  newApp: NewAppTarget
}

interface DeployTargetPickerProps {
  value: DeployTargetValue
  onChange: (patch: Partial<Pick<DeployTargetValue, 'mode' | 'existingApp'>>) => void
  onNewApp: (patch: Partial<NewAppTarget>) => void
  apps: ExistingApp[]
  appsLoading?: boolean
  resourceGroups: ResourceGroupOption[]
  rgLoading?: boolean
  regions: string[]
  radioGroup?: string
  modelHint?: string
}

export const DeployTargetPicker = ({
  value,
  onChange,
  onNewApp,
  apps,
  appsLoading = false,
  resourceGroups,
  rgLoading = false,
  regions,
  radioGroup = 'deploy-target',
  modelHint,
}: DeployTargetPickerProps) => {
  const { mode, existingApp, newApp } = value
  return (
    <>
      <label className="check">
        <input
          type="radio"
          name={radioGroup}
          checked={mode === 'existing'}
          onChange={() => onChange({ mode: 'existing' })}
        />{' '}
        Add to an existing AI App
      </label>
      {mode === 'existing' && (
        <div className="field indent-field">
          <select value={existingApp} onChange={(e) => onChange({ existingApp: e.target.value })}>
            <option value="">
              {appsLoading
                ? 'Loading apps…'
                : apps.length
                  ? 'Select a Function App…'
                  : 'No AI Apps in this subscription'}
            </option>
            {apps.map((a) => (
              <option key={a.name} value={a.name}>
                {a.name} ({a.resourceGroup})
              </option>
            ))}
          </select>
          <div className="hint">One Function App can host many agents.</div>
        </div>
      )}

      <label className="check">
        <input
          type="radio"
          name={radioGroup}
          checked={mode === 'new'}
          onChange={() => onChange({ mode: 'new' })}
        />{' '}
        Create a new AI App (Function App, Flex Consumption)
      </label>
      {mode === 'new' && (
        <div className="indent-block">
          <div className="grid cols-2" style={{ gap: 12 }}>
            <div className="field">
              <label>Function App name</label>
              <input
                type="text"
                value={newApp.appName}
                placeholder="func-my-agents"
                onChange={(e) => onNewApp({ appName: e.target.value })}
              />
              <div className="hint">Globally unique across *.azurewebsites.net.</div>
            </div>
            <div className="field">
              <label>Region</label>
              <select value={newApp.region} onChange={(e) => onNewApp({ region: e.target.value })}>
                {regions.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Resource group</label>
            <div className="rg-mode-row">
              <label className="check inline">
                <input
                  type="radio"
                  name={`${radioGroup}-rg`}
                  checked={newApp.rgMode === 'existing'}
                  onChange={() => onNewApp({ rgMode: 'existing' })}
                />{' '}
                Use existing
              </label>
              <label className="check inline">
                <input
                  type="radio"
                  name={`${radioGroup}-rg`}
                  checked={newApp.rgMode === 'new'}
                  onChange={() => onNewApp({ rgMode: 'new' })}
                />{' '}
                Create new
              </label>
            </div>
            {newApp.rgMode === 'existing' ? (
              <select
                value={newApp.resourceGroup}
                onChange={(e) => onNewApp({ resourceGroup: e.target.value })}
              >
                <option value="">
                  {rgLoading
                    ? 'Loading resource groups…'
                    : resourceGroups.length
                      ? 'Select a resource group…'
                      : 'No resource groups found'}
                </option>
                {resourceGroups.map((g) => (
                  <option key={g.name} value={g.name}>
                    {g.name} · {g.location}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={newApp.resourceGroup}
                placeholder="rg-my-agents"
                onChange={(e) => onNewApp({ resourceGroup: e.target.value })}
              />
            )}
          </div>
          {modelHint && (
            <div className="hint" style={{ marginTop: 8 }}>
              Reuses the Foundry model from step 1 (<span className="mono">{modelHint || '—'}</span>) — no
              Foundry is provisioned.
            </div>
          )}
        </div>
      )}
    </>
  )
}
