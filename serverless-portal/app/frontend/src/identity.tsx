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

interface IdentityState {
  identity: Identity | null
  subscriptions: Subscription[]
  selected: string
  setSelected: (id: string) => void
  loading: boolean
  error: string | null
}

const IdentityContext = createContext<IdentityState | null>(null)

export function IdentityProvider({ children }: { children: ReactNode }) {
  const identityQuery = useQuery({
    queryKey: queryKeys.identity,
    queryFn: () => api.identity(),
    staleTime: staleTimes.identity,
  })
  const subsQuery = useQuery({
    queryKey: queryKeys.subscriptions,
    queryFn: () => api.listSubscriptions(),
    staleTime: staleTimes.subscriptions,
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

  // Once identity + subscriptions load, pick a default if the persisted
  // selection is empty or no longer valid for this tenant.
  useEffect(() => {
    if (!identity || subscriptions.length === 0) return
    if (selected && subscriptions.some((s) => s.id === selected)) return
    const preferred = subscriptions.some((s) => s.id === identity.subscription.id)
      ? identity.subscription.id
      : (subscriptions[0]?.id ?? identity.subscription.id)
    setSelected(preferred)
  }, [identity, subscriptions, selected, setSelected])

  const loading = identityQuery.isLoading || subsQuery.isLoading
  const error =
    (identityQuery.error as Error | null)?.message ??
    (subsQuery.error as Error | null)?.message ??
    null

  const value = useMemo<IdentityState>(
    () => ({ identity, subscriptions, selected, setSelected, loading, error }),
    [identity, subscriptions, selected, setSelected, loading, error],
  )

  return <IdentityContext.Provider value={value}>{children}</IdentityContext.Provider>
}

export function useIdentity(): IdentityState {
  const ctx = useContext(IdentityContext)
  if (!ctx) throw new Error('useIdentity must be used within an IdentityProvider')
  return ctx
}
