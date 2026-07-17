// Sign-in gate shown to unauthenticated users. Sign-in is only ever started by
// the user clicking the button (never automatically).

import { useState } from 'react'
import { signIn } from '../auth'

export default function LoginPage() {
  const [busy, setBusy] = useState(false)

  const onSignIn = async () => {
    setBusy(true)
    try {
      await signIn()
    } catch {
      // A failed redirect kick-off leaves us on the login page; re-enable.
      setBusy(false)
    }
  }

  return (
    <div className="login">
      <div className="login-card">
        <div className="login-mark">⚡</div>
        <h1>Serverless Agent Portal</h1>
        <p>Sign in with your Microsoft account to discover serverless agents in your subscriptions.</p>
        <button className="btn primary" onClick={onSignIn} disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </div>
    </div>
  )
}
