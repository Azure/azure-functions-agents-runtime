import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { AuthenticatedTemplate, MsalProvider, UnauthenticatedTemplate } from '@azure/msal-react'
import App from './App'
import LoginPage from './pages/LoginPage'
import { IdentityProvider } from './identity'
import { initMsal } from './auth'
import './styles.css'

initMsal().then((instance) => {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <MsalProvider instance={instance}>
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
      </MsalProvider>
    </React.StrictMode>,
  )
})
