// Reusable pill badge. Presentational only — depends on the shared design-system
// classes in styles.css, so it can be lifted into any app that ships those tokens.

import type { ReactNode } from 'react'

export type BadgeTone = 'green' | 'amber' | 'red' | 'blue' | 'gray' | 'purple'

interface BadgeProps {
  tone?: BadgeTone
  dot?: boolean
  title?: string
  className?: string
  children: ReactNode
}

export const Badge = ({ tone = 'gray', dot = false, title, className, children }: BadgeProps) => (
  <span className={`badge ${tone}${className ? ` ${className}` : ''}`} title={title}>
    {dot && <span className="dot" />}
    {children}
  </span>
)
