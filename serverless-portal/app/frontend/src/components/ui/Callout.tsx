// An informational callout card with a leading icon and a title/body. Used for
// the "how AI Apps are identified" explainer on the dashboard and create flow.

import type { ReactNode } from 'react'

interface CalloutProps {
  icon?: ReactNode
  title?: ReactNode
  children?: ReactNode
  className?: string
}

export const Callout = ({ icon = '🔖', title, children, className }: CalloutProps) => (
  <div className={`card callout${className ? ` ${className}` : ''}`}>
    <span className="ico">{icon}</span>
    <div>
      {title && <b>{title}</b>}
      {children}
    </div>
  </div>
)
