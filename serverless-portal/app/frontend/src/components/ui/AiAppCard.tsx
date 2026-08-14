// A card representing one AI App — an Azure Function App identified by the
// `AZURE_FUNCTIONS_AGENTS_PROVIDER` app setting (its value is the model
// `provider`). Renders the app header, the agent-runtime marker, a composition
// summary, and its agents. Agent links are supplied via a render prop so this
// stays router-agnostic and migratable.

import type { ReactNode } from 'react'
import { StatTiles } from './StatTiles'
import { StatusBadge } from './StatusBadge'

export interface AiAppAgent {
  name: string
  trigger: string
  builtinEndpoints?: boolean
}

export interface AiApp {
  name: string
  resourceGroup: string
  location: string
  provider: string
  defaultHostName?: string
  agents: AiAppAgent[]
  supportingFunctions?: { name: string; trigger: string }[]
}

interface AiAppCardProps {
  app: AiApp
  status?: string
  renderAgent?: (agent: AiAppAgent) => ReactNode
  renderAppLink?: (children: ReactNode) => ReactNode
  actions?: ReactNode
}

export const AiAppCard = ({ app, status = 'running', renderAgent, renderAppLink, actions }: AiAppCardProps) => {
  const builtins = app.agents.filter((a) => a.builtinEndpoints).length
  const supporting = app.supportingFunctions?.length ?? 0
  return (
    <div className="card ai-app-card">
      <div className="card-head">
        <h3 className="mono" title={app.name}>
          {renderAppLink ? renderAppLink(app.name) : app.name}
        </h3>
        <StatusBadge status={status} />
      </div>
      <StatTiles
        items={[
          { n: app.agents.length, label: app.agents.length === 1 ? 'Agent' : 'Agents' },
          { n: builtins, label: 'Built-in' },
          { n: supporting, label: 'Tools/triggers' },
        ]}
      />
      {app.agents.length > 0 && (
        <>
          <div className="divider ai-app-divider" />
          <div className="group-sub">Agents</div>
          <div className="ai-app-agents">
            {app.agents.map((a) => (
              <div className="ai-app-agent" key={a.name}>
                {renderAgent ? renderAgent(a) : <span className="mono">{a.name}.agent.md</span>}
              </div>
            ))}
          </div>
        </>
      )}
      {actions && (
        <div className="pill-row ai-app-actions">{actions}</div>
      )}
    </div>
  )
}
