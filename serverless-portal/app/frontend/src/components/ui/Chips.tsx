// Horizontal wrap of small pills/badges.

import type { CSSProperties, ReactNode } from 'react'

export const Chips = ({ children, style }: { children: ReactNode; style?: CSSProperties }) => (
  <div className="chips" style={style}>
    {children}
  </div>
)
