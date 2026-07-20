import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { AuthenticatedTemplate, MsalProvider, UnauthenticatedTemplate } from '@azure/msal-react'
import { QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import LoginPage from './pages/LoginPage'
import { IdentityProvider } from './identity'
import { initMsal } from './auth'
import { createQueryClient } from './query'
import './styles.css'

const queryClient = createQueryClient()

initMsal().then((instance) => {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <MsalProvider instance={instance}>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <AuthenticatedTemplate>
              <IdentityProvider>
                <App />
              </IdentityProvider>
            </AuthenticatedTemplate>
            <UnauthenticatedTemplate>
              <LoginPage />
            </UnauthenticatedTemplate>
          </BrowserRouter>
        </QueryClientProvider>
      </MsalProvider>
    </React.StrictMode>,
  )
})
