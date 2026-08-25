import React, { useSyncExternalStore } from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { MsalProvider, useIsAuthenticated } from '@azure/msal-react'
import { QueryClientProvider } from '@tanstack/react-query'
import { CoreAIFluentProvider, coreaiDarkTheme, coreaiLightTheme } from '@coreai/fluentui-react'
import App from './App'
import LoginPage from './pages/LoginPage'
import { IdentityProvider } from './identity'
import { initMsal, getManualToken, subscribeManualToken } from './auth'
import { createQueryClient } from './query'
import { useThemeMode } from './theme'
import { DeployProvider } from './deploy'
import '@fontsource-variable/inter'
import '@coreai/fluentui-react/fonts/fonts.css'
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
        <DeployProvider>
          <App />
        </DeployProvider>
      </IdentityProvider>
    )
  }
  return <LoginPage />
}

// Root CoreAI theme provider — applies the CoreAI Fluent v9 theme (brand ramp,
// Aptos typography, light/dark token overrides) to the whole app. Background is
// left transparent so the existing page chrome shows through during migration.
function ThemedApp() {
  const mode = useThemeMode()
  return (
    <CoreAIFluentProvider
      className="coreai-root"
      theme={mode === 'dark' ? coreaiDarkTheme : coreaiLightTheme}
      style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh', background: 'transparent' }}
    >
      <AuthGate />
    </CoreAIFluentProvider>
  )
}

initMsal().then((instance) => {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <MsalProvider instance={instance}>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <ThemedApp />
          </BrowserRouter>
        </QueryClientProvider>
      </MsalProvider>
    </React.StrictMode>,
  )
})
