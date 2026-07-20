// React Query configuration — cache keys and stale times for the portal's
// live Azure data. Mirrors the Cascade Portal (Polaris) caching approach:
// identity/subscriptions rarely change (longer stale window); live agent
// discovery is scoped per subscription and refreshed more eagerly.

import { QueryClient } from '@tanstack/react-query'
import { ApiError, type LiveDiscovery } from './api'

export const queryKeys = {
  identity: ['identity'] as const,
  subscriptions: ['subscriptions'] as const,
  liveAgents: (subscriptionId: string) => ['live-agents', subscriptionId] as const,
}

export const staleTimes = {
  identity: 5 * 60_000, // 5 min
  subscriptions: 5 * 60_000, // 5 min
  liveAgents: 60_000, // 60 s
}

// Per-subscription persistence of discovered agents so a shared/reloaded
// deeplink (`/agents/:subscriptionId`) hydrates instantly from cache instead of
// re-scanning. A fresh scan only happens on first load or an explicit refresh.
const AGENTS_CACHE_PREFIX = 'serverless-portal:agents:'

export interface AgentsSnapshot {
  data: LiveDiscovery
  updatedAt: number
}

export function readAgentsSnapshot(subscriptionId: string): AgentsSnapshot | undefined {
  if (!subscriptionId) return undefined
  try {
    const raw = localStorage.getItem(AGENTS_CACHE_PREFIX + subscriptionId)
    if (!raw) return undefined
    const parsed = JSON.parse(raw) as AgentsSnapshot
    if (!parsed || typeof parsed.updatedAt !== 'number' || !parsed.data) return undefined
    return parsed
  } catch {
    return undefined
  }
}

export function writeAgentsSnapshot(subscriptionId: string, data: LiveDiscovery, updatedAt: number): void {
  if (!subscriptionId) return
  try {
    localStorage.setItem(
      AGENTS_CACHE_PREFIX + subscriptionId,
      JSON.stringify({ data, updatedAt } satisfies AgentsSnapshot),
    )
  } catch {
    /* storage full / disabled — cache stays in memory only */
  }
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Keep cached data around for 30 min so navigating back renders
        // instantly while a background refetch reconciles.
        gcTime: 30 * 60_000,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          // 4xx are terminal (unauthorized / forbidden / not-found) — a
          // retry returns the same response. Only retry transient failures.
          if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
            return false
          }
          return failureCount < 1
        },
      },
    },
  })
}
