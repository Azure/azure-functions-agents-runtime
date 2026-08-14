// Reusable "where does this deploy?" chooser: add to an existing Function App,
// or create a new Flex Consumption one. Controlled + prop-driven so the create
// flow and any future redeploy flow can share it. Emits partial patches.

import { SearchableSelect } from './SearchableSelect'

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
  lockAppName?: boolean
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
  lockAppName = false,
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
          <SearchableSelect
            value={existingApp}
            onChange={(v) => onChange({ existingApp: v })}
            options={apps.map((a) => ({ value: a.name, label: a.name, sublabel: a.resourceGroup }))}
            placeholder={appsLoading ? 'Loading apps…' : apps.length ? 'Select a Function App…' : 'No AI Apps in this subscription'}
            loading={appsLoading}
            ariaLabel="Existing Function App"
          />
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
                disabled={lockAppName}
                onChange={(e) => onNewApp({ appName: e.target.value })}
              />
              <div className="hint">
                {lockAppName
                  ? 'Locked — capabilities are attached to this app name.'
                  : 'Globally unique across *.azurewebsites.net.'}
              </div>
            </div>
            <div className="field">
              <label>Region</label>
              <SearchableSelect
                value={newApp.region}
                onChange={(v) => onNewApp({ region: v })}
                options={regions.map((r) => ({ value: r, label: r }))}
                placeholder="Select a region…"
                ariaLabel="Region"
              />
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
              <SearchableSelect
                value={newApp.resourceGroup}
                onChange={(v) => onNewApp({ resourceGroup: v })}
                options={resourceGroups.map((g) => ({ value: g.name, label: g.name, sublabel: g.location }))}
                placeholder={rgLoading ? 'Loading resource groups…' : resourceGroups.length ? 'Select a resource group…' : 'No resource groups found'}
                loading={rgLoading}
                ariaLabel="Resource group"
              />
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
