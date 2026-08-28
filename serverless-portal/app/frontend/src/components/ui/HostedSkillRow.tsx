import type { ReactNode } from 'react'
import { StatusBadge } from './StatusBadge'

export interface HostedSkillSummary {
  name: string
  trigger: string
  builtinEndpoints?: boolean
}

export interface HostedSkillApp {
  name: string
  resourceGroup: string
  location: string
  provider: string
  defaultHostName?: string
  agents: HostedSkillSummary[]
  supportingFunctions?: { name: string; trigger: string }[]
}

interface HostedSkillRowProps {
  app: HostedSkillApp
  status?: string
  renderAppLink?: (children: ReactNode) => ReactNode
  actions?: ReactNode
}

export const HostedSkillRow = ({ app, status = 'running', renderAppLink, actions }: HostedSkillRowProps) => {
  const primarySkill = app.agents[0]
  const skillSummary = primarySkill
    ? `${primarySkill.name}${app.agents.length > 1 ? ` +${app.agents.length - 1}` : ''}`
    : 'No Hosted Skills'
  const content = (
    <>
      <span className="hosted-skill-app">
        <strong>{app.name}</strong>
        <small className="mono">{app.resourceGroup}</small>
      </span>
      <span className="hosted-skill-count">
        <strong>{app.agents.length}</strong>
        <small>{skillSummary}</small>
      </span>
      <span className="hosted-skill-model">
        <strong>{app.provider || 'Default model'}</strong>
        <small>Microsoft Foundry</small>
      </span>
      <span className="hosted-skill-region">{app.location || 'Unknown'}</span>
      <span className="hosted-skill-health"><StatusBadge status={status} /></span>
      <span className="hosted-skill-open" aria-hidden="true">›</span>
    </>
  )

  return (
    <div className="hosted-skill-row">
      {renderAppLink ? renderAppLink(content) : <div className="hosted-skill-row-content">{content}</div>}
      {actions && <div className="pill-row ai-app-actions">{actions}</div>}
    </div>
  )
}
