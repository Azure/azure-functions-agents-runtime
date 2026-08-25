// Signed-in identity, the subscriptions the user can see, and the currently
// selected subscription (which drives live agent discovery). The user signs in
// via MSAL; the forwarded ARM token authorises every backend call.
//
// Identity and the subscription list are cached with React Query (see
// ./query), so they survive navigation and are only refetched once stale. The
// selected subscription is persisted to localStorage.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type Identity, type Subscription } from './api'
import { queryKeys, staleTimes } from './query'

const SELECTED_SUB_KEY = 'serverless-portal:selected-subscription'
const SUBSCRIPTIONS_CACHE_KEY = 'serverless-portal:subscriptions'

interface SubscriptionsSnapshot {
  subscriptions: Subscription[]
  updatedAt: number
}

function readSubscriptionsSnapshot(): SubscriptionsSnapshot | undefined {
  try {
    const raw = localStorage.getItem(SUBSCRIPTIONS_CACHE_KEY)
    if (!raw) return undefined
    const parsed = JSON.parse(raw) as Partial<SubscriptionsSnapshot>
    if (!Array.isArray(parsed.subscriptions) || typeof parsed.updatedAt !== 'number') return undefined
    return { subscriptions: parsed.subscriptions, updatedAt: parsed.updatedAt }
  } catch {
    return undefined
  }
}

function writeSubscriptionsSnapshot(subscriptions: Subscription[], updatedAt: number): void {
  try {
    localStorage.setItem(
      SUBSCRIPTIONS_CACHE_KEY,
      JSON.stringify({ subscriptions, updatedAt } satisfies SubscriptionsSnapshot),
    )
  } catch {
    /* storage full / disabled — React Query still retains the list in memory */
  }
}

interface IdentityState {
  identity: Identity | null
  subscriptions: Subscription[]
  selected: string
  setSelected: (id: string) => void
  loading: boolean
  error: string | null
  subscriptionsLoading: boolean
  subscriptionsRefreshing: boolean
  subscriptionError: string | null
  refreshSubscriptions: () => Promise<void>
}

const IdentityContext = createContext<IdentityState | null>(null)

export function IdentityProvider({ children }: { children: ReactNode }) {
  const [subscriptionsSnapshot] = useState(readSubscriptionsSnapshot)
  const identityQuery = useQuery({
    queryKey: queryKeys.identity,
    queryFn: () => api.identity(),
    staleTime: staleTimes.identity,
  })
  const subsQuery = useQuery({
    queryKey: queryKeys.subscriptions,
    queryFn: () => api.listSubscriptions(),
    staleTime: staleTimes.subscriptions,
    initialData: subscriptionsSnapshot?.subscriptions,
    initialDataUpdatedAt: subscriptionsSnapshot?.updatedAt,
    refetchOnMount: 'always',
    refetchOnReconnect: true,
    refetchOnWindowFocus: true,
  })

  const identity = identityQuery.data ?? null
  const subscriptions = useMemo(() => subsQuery.data ?? [], [subsQuery.data])

  const [selected, setSelectedState] = useState<string>(() => {
    try {
      return localStorage.getItem(SELECTED_SUB_KEY) ?? ''
    } catch {
      return ''
    }
  })

  const setSelected = useCallback((id: string) => {
    setSelectedState(id)
    try {
      localStorage.setItem(SELECTED_SUB_KEY, id)
    } catch {
      /* private mode / storage disabled — selection stays in memory */
    }
  }, [])

  useEffect(() => {
    if (subsQuery.data && subsQuery.dataUpdatedAt > 0) {
      writeSubscriptionsSnapshot(subsQuery.data, subsQuery.dataUpdatedAt)
    }
  }, [subsQuery.data, subsQuery.dataUpdatedAt])

  // Restore a valid selection from the subscription list itself. The identity
  // request can fail when its configured default subscription is inaccessible,
  // but that must not leave an otherwise healthy picker unusable.
  useEffect(() => {
    if (subscriptions.length === 0) return
    if (selected && subscriptions.some((s) => s.id === selected)) return
    const preferred = identity && subscriptions.some((s) => s.id === identity.subscription.id)
      ? identity.subscription.id
      : subscriptions[0].id
    setSelected(preferred)
  }, [identity, subscriptions, selected, setSelected])

  const loading = identityQuery.isLoading || subsQuery.isLoading
  const subscriptionsLoading = subsQuery.isLoading && subscriptions.length === 0
  const subscriptionsRefreshing = subsQuery.isFetching
  const subscriptionError = (subsQuery.error as Error | null)?.message ?? null
  const refreshSubscriptions = useCallback(async () => {
    await subsQuery.refetch()
  }, [subsQuery.refetch])
  const error =
    (identityQuery.error as Error | null)?.message ??
    (subsQuery.error as Error | null)?.message ??
    null

  const value = useMemo<IdentityState>(
    () => ({
      identity,
      subscriptions,
      selected,
      setSelected,
      loading,
      error,
      subscriptionsLoading,
      subscriptionsRefreshing,
      subscriptionError,
      refreshSubscriptions,
    }),
    [
      identity,
      subscriptions,
      selected,
      setSelected,
      loading,
      error,
      subscriptionsLoading,
      subscriptionsRefreshing,
      subscriptionError,
      refreshSubscriptions,
    ],
  )

  return <IdentityContext.Provider value={value}>{children}</IdentityContext.Provider>
}

export function useIdentity(): IdentityState {
  const ctx = useContext(IdentityContext)
  if (!ctx) throw new Error('useIdentity must be used within an IdentityProvider')
  return ctx
}
