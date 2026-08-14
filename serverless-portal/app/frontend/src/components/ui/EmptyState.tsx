// Centered empty-state message for lists/grids with no results.

import type { ReactNode } from 'react'

export const EmptyState = ({ children }: { children: ReactNode }) => <div className="empty">{children}</div>
