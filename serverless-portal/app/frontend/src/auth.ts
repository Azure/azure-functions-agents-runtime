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

// Same first-party app as Polaris (for now). Overridable via /api/auth/config.
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

/** Sign the user out and return to the app origin. */
export async function signOut(): Promise<void> {
  await msal().logoutRedirect()
}

/**
 * Acquire an ARM access token for the signed-in user, silently when possible.
 * Falls back to an interactive redirect when consent/interaction is required.
 */
export async function acquireArmToken(): Promise<string> {
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
