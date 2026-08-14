// Sign-in gate shown to unauthenticated users. Sign-in is only ever started by
// the user clicking the button (never automatically). A fallback lets a user
// paste an ARM access token to try the portal without the MSAL redirect.

import { useState } from 'react'
import { signIn, setManualToken, validateArmToken } from '../auth'
import { CopyButton } from '../components/SourceEditor'

export default function LoginPage() {
  const [busy, setBusy] = useState(false)
  const [showToken, setShowToken] = useState(false)
  const [token, setToken] = useState('')
  const [tokenError, setTokenError] = useState<string | null>(null)

  const onSignIn = async () => {
    setBusy(true)
    try {
      await signIn()
    } catch {
      // A failed redirect kick-off leaves us on the login page; re-enable.
      setBusy(false)
    }
  }

  const onUseToken = () => {
    const result = validateArmToken(token)
    if (!result.ok) {
      setTokenError(result.error)
      return
    }
    setTokenError(null)
    // Flips the auth gate (useSyncExternalStore) → the app loads with this token.
    setManualToken(token)
  }

  const cmd = 'az account get-access-token --resource https://management.azure.com --query accessToken -o tsv'

  return (
    <div className="login">
      <div className="login-card">
        <div className="login-mark">⚡</div>
        <h1>AI Apps</h1>
        <p>Sign in with your Microsoft account to discover serverless agents in your subscriptions.</p>
        <button className="btn primary" onClick={onSignIn} disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>

        <div className="login-alt">
          <button className="btn ghost sm" onClick={() => setShowToken((s) => !s)}>
            {showToken ? 'Hide token option' : 'No sign-in? Use an ARM token'}
          </button>
        </div>

        {showToken && (
          <div className="login-token">
            <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
              Paste an Azure Resource Manager token to try the portal without signing in. Get one with:
            </p>
            <div className="login-token-cmd-row">
              <code className="login-token-cmd">{cmd}</code>
              <CopyButton text={cmd} title="Copy the command" />
            </div>
            <textarea
              className="editor"
              style={{ minHeight: 92, fontSize: 12 }}
              spellCheck={false}
              placeholder="Paste the eyJ… access token"
              value={token}
              onChange={(e) => {
                setToken(e.target.value)
                setTokenError(null)
              }}
              aria-label="ARM access token"
            />
            <button
              className="btn primary"
              disabled={!token.trim()}
              onClick={onUseToken}
              style={{ marginTop: 8 }}
            >
              Continue with token
            </button>
            {tokenError && (
              <p className="muted" style={{ color: 'var(--red)', fontSize: 12, marginTop: 8 }}>
                {tokenError}
              </p>
            )}
            <p className="muted" style={{ fontSize: 11, marginTop: 10 }}>
              ⚠️ An ARM token grants full access as you for ~1 hour. It’s kept only in this browser tab and
              sent only to this portal’s backend. Don’t paste tokens into sites you don’t trust.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
