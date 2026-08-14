import React, { useSyncExternalStore } from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { MsalProvider, useIsAuthenticated } from '@azure/msal-react'
import { QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import LoginPage from './pages/LoginPage'
import { IdentityProvider } from './identity'
import { initMsal, getManualToken, subscribeManualToken } from './auth'
import { createQueryClient } from './query'
import '@fontsource-variable/inter'
import './theme'
import './styles.css'

const queryClient = createQueryClient()

// Render App when signed in via MSAL, OR when a manual ARM token (Option C —
// paste-a-token) is present. Either path yields an ARM bearer token for the API.
function AuthGate() {
  const msalAuthed = useIsAuthenticated()
  const manualToken = useSyncExternalStore(subscribeManualToken, getManualToken)
  if (msalAuthed || manualToken) {
    return (
      <IdentityProvider>
        <App />
      </IdentityProvider>
    )
  }
  return <LoginPage />
}

initMsal().then((instance) => {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <MsalProvider instance={instance}>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <AuthGate />
          </BrowserRouter>
        </QueryClientProvider>
      </MsalProvider>
    </React.StrictMode>,
  )
})
