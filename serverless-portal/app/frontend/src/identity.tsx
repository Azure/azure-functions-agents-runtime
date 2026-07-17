// Signed-in identity, the subscriptions the user can see, and the currently
// selected subscription (which drives live agent discovery). The user signs in
// via MSAL; the forwarded ARM token authorises every backend call.

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, type Identity, type Subscription } from './api'

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
  const [identity, setIdentity] = useState<Identity | null>(null)
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([])
  const [selected, setSelected] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    Promise.all([api.identity(), api.listSubscriptions()])
      .then(([id, subs]) => {
        if (cancelled) return
        setIdentity(id)
        setSubscriptions(subs)
        // Default to the backend's subscription; fall back to the first listed.
        const preferred = subs.some((s) => s.id === id.subscription.id)
          ? id.subscription.id
          : (subs[0]?.id ?? id.subscription.id)
        setSelected(preferred)
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const value = useMemo<IdentityState>(
    () => ({ identity, subscriptions, selected, setSelected, loading, error }),
    [identity, subscriptions, selected, loading, error],
  )

  return <IdentityContext.Provider value={value}>{children}</IdentityContext.Provider>
}

export function useIdentity(): IdentityState {
  const ctx = useContext(IdentityContext)
  if (!ctx) throw new Error('useIdentity must be used within an IdentityProvider')
  return ctx
}
