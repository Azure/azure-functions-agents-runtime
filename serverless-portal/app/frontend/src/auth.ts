// Browser sign-in for the Serverless Agent Portal.
//
// Mirrors the Polaris flow: a public-client (SPA) MSAL app using the same
// first-party app registration, redirect-based sign-in, and ARM consent
// obtained up front so the ARM access token can be acquired silently and
// forwarded to the backend on every API call.

import {
  EventType,
  InteractionRequiredAuthError,
  PublicClientApplication,
} from '@azure/msal-browser'
import type { Configuration, RedirectRequest } from '@azure/msal-browser'

// Local-dev default: Polaris's already-tenant-consented app (works without admin
// consent). Deploys override via /api/auth/config -> MSAL_CLIENT_ID env var.
const DEFAULT_CLIENT_ID = '409cf302-c83f-43c3-94eb-ca581ab18c6d'
const DEFAULT_AUTHORITY = 'https://login.microsoftonline.com/organizations'

// ARM scope — identical to Polaris. Consent is captured at sign-in so the token
// can later be acquired silently for API calls.
export const ARM_SCOPE = 'https://management.core.windows.net/.default'

// Sign-in request: identity + ARM consent up front.
export const loginRequest: RedirectRequest = {
  scopes: ['openid', 'profile', ARM_SCOPE],
}

// Token request for backend calls.
const armRequest = { scopes: [ARM_SCOPE] }

// ---------------------------------------------------------------------------
// Manual ARM token (Option C) — try the portal without the interactive MSAL
// sign-in by pasting an ARM access token (e.g. from
// `az account get-access-token --resource https://management.azure.com`). Held
// in sessionStorage (per-tab, cleared on tab close); when present it takes
// precedence over MSAL for every backend call. It is a powerful bearer
// credential, so it is never logged and never persisted beyond the tab.
// ---------------------------------------------------------------------------

const MANUAL_TOKEN_KEY = 'serverless-portal:arm-token'
const manualTokenListeners = new Set<() => void>()

function emitManualToken() {
  for (const l of manualTokenListeners) l()
}

/** Subscribe to manual-token changes (drives useSyncExternalStore in the gate). */
export function subscribeManualToken(cb: () => void): () => void {
  manualTokenListeners.add(cb)
  return () => {
    manualTokenListeners.delete(cb)
  }
}

/** The current manually-pasted ARM token, or null. */
export function getManualToken(): string | null {
  try {
    return sessionStorage.getItem(MANUAL_TOKEN_KEY)
  } catch {
    return null
  }
}

/** Store a pasted ARM token for this tab and notify listeners. */
export function setManualToken(token: string): void {
  try {
    sessionStorage.setItem(MANUAL_TOKEN_KEY, token.trim())
  } catch {
    /* storage unavailable — non-fatal */
  }
  emitManualToken()
}

/** Forget the pasted ARM token (sign out / expiry) and notify listeners. */
export function clearManualToken(): void {
  try {
    sessionStorage.removeItem(MANUAL_TOKEN_KEY)
  } catch {
    /* ignore */
  }
  emitManualToken()
}

export interface ArmTokenClaims {
  name: string
  username: string
  oid: string
  tenantId: string
  audience: string
  expiresAt: number
}

/** Decode (without verifying) the claims of a JWT ARM access token. */
export function decodeArmToken(token: string): ArmTokenClaims | null {
  const parts = token.trim().split('.')
  if (parts.length !== 3) return null
  try {
    const json = atob(parts[1].replace(/-/g, '+').replace(/_/g, '/'))
    const c = JSON.parse(json) as Record<string, unknown>
    return {
      name: String(c.name ?? ''),
      username: String(c.upn ?? c.unique_name ?? c.preferred_username ?? ''),
      oid: String(c.oid ?? ''),
      tenantId: String(c.tid ?? ''),
      audience: String(c.aud ?? ''),
      expiresAt: Number(c.exp ?? 0),
    }
  } catch {
    return null
  }
}

/** Validate a pasted string looks like a non-expired ARM access token. */
export function validateArmToken(
  token: string,
): { ok: true; claims: ArmTokenClaims } | { ok: false; error: string } {
  const claims = decodeArmToken(token)
  if (!claims) {
    return { ok: false, error: 'That doesn’t look like a token — paste the full JWT (three dot-separated parts).' }
  }
  const aud = claims.audience.toLowerCase()
  if (!aud.includes('management.azure.com') && !aud.includes('management.core.windows.net')) {
    return {
      ok: false,
      error: `This token’s audience is “${claims.audience || 'unknown'}”, not Azure Resource Manager. Use --resource https://management.azure.com.`,
    }
  }
  if (claims.expiresAt && claims.expiresAt * 1000 <= Date.now()) {
    return { ok: false, error: 'This token has expired — run the command again for a fresh one.' }
  }
  return { ok: true, claims }
}

interface RuntimeAuthConfig {
  clientId: string
  authority: string
}

async function loadRuntimeConfig(): Promise<RuntimeAuthConfig> {
  try {
    const res = await fetch('/api/auth/config', { cache: 'no-store' })
    if (!res.ok) return { clientId: DEFAULT_CLIENT_ID, authority: DEFAULT_AUTHORITY }
    const data = (await res.json()) as { msalClientId?: string; msalAuthority?: string }
    return {
      clientId: (data.msalClientId || DEFAULT_CLIENT_ID).trim(),
      authority: (data.msalAuthority || DEFAULT_AUTHORITY).trim(),
    }
  } catch {
    return { clientId: DEFAULT_CLIENT_ID, authority: DEFAULT_AUTHORITY }
  }
}

function createConfig(rt: RuntimeAuthConfig): Configuration {
  return {
    auth: {
      clientId: rt.clientId,
      authority: rt.authority,
      knownAuthorities: ['login.microsoftonline.com'],
      redirectUri: window.location.origin,
      postLogoutRedirectUri: window.location.origin,
    },
    cache: {
      // Per-tab, cleared on tab close. Required for the redirect flow.
      cacheLocation: 'sessionStorage',
      storeAuthStateInCookie: false,
    },
  }
}

let instance: PublicClientApplication | null = null

/** The initialized MSAL instance. Throws if `initMsal()` has not completed. */
export function msal(): PublicClientApplication {
  if (!instance) throw new Error('MSAL has not been initialized.')
  return instance
}

/**
 * Create + initialize the MSAL instance and process any pending redirect.
 * Call once before rendering; the resolved instance feeds `<MsalProvider>`.
 */
export async function initMsal(): Promise<PublicClientApplication> {
  const rt = await loadRuntimeConfig()
  const msalInstance = new PublicClientApplication(createConfig(rt))
  await msalInstance.initialize()

  const accounts = msalInstance.getAllAccounts()
  if (accounts.length > 0) msalInstance.setActiveAccount(accounts[0])

  msalInstance.addEventCallback((event) => {
    if (
      event.eventType === EventType.LOGIN_SUCCESS &&
      event.payload &&
      'account' in event.payload &&
      event.payload.account
    ) {
      msalInstance.setActiveAccount(event.payload.account)
    }
  })

  // Complete the return leg of a redirect sign-in, if any.
  await msalInstance.handleRedirectPromise()

  instance = msalInstance
  return msalInstance
}

/** Start an interactive redirect sign-in. Only call from a user action. */
export async function signIn(): Promise<void> {
  await msal().loginRedirect(loginRequest)
}

/** Sign the user out and return to the app origin. Clears a pasted token too. */
export async function signOut(): Promise<void> {
  const hadManual = !!getManualToken()
  clearManualToken()
  let account = null
  try {
    account = msal().getActiveAccount() ?? msal().getAllAccounts()[0]
  } catch {
    account = null
  }
  if (account) {
    await msal().logoutRedirect()
    return
  }
  // Manual-token-only session: reload to the sign-in gate with a clean slate.
  if (hadManual) window.location.assign('/')
}

/**
 * Acquire an ARM access token for the signed-in user, silently when possible.
 * Falls back to an interactive redirect when consent/interaction is required.
 */
export async function acquireArmToken(): Promise<string> {
  // Option C: a pasted ARM token takes precedence over MSAL when present.
  const manual = getManualToken()
  if (manual) return manual
  const msalInstance = msal()
  const account = msalInstance.getActiveAccount() ?? msalInstance.getAllAccounts()[0]
  if (!account) throw new Error('Not signed in.')
  try {
    const res = await msalInstance.acquireTokenSilent({ ...armRequest, account })
    return res.accessToken
  } catch (err) {
    if (err instanceof InteractionRequiredAuthError) {
      await msalInstance.acquireTokenRedirect(loginRequest)
    }
    throw err
  }
}
