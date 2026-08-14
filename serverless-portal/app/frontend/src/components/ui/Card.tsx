// Surface container with an optional header row (title + actions). Wraps the
// shared `.card` / `.card-head` classes so pages don't repeat the markup.

import type { CSSProperties, ReactNode } from 'react'

interface CardProps {
  title?: ReactNode
  actions?: ReactNode
  className?: string
  style?: CSSProperties
  children: ReactNode
}

export const Card = ({ title, actions, className, style, children }: CardProps) => (
  <div className={`card${className ? ` ${className}` : ''}`} style={style}>
    {(title || actions) && (
      <div className="card-head">
        {typeof title === 'string' ? <h3>{title}</h3> : title}
        {actions}
      </div>
    )}
    {children}
  </div>
)
