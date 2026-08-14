import { Routes, Route, Navigate } from 'react-router-dom'
import Shell from './components/Shell'
import AgentsPage from './pages/AgentsPage'
import AgentDetailPage from './pages/AgentDetailPage'
import AppDetailPage from './pages/AppDetailPage'
import CreateAgentPage from './pages/CreateAgentPage'
import DraftAppPage from './pages/DraftAppPage'
import PlaygroundPage from './pages/PlaygroundPage'

export default function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Navigate to="/agents" replace />} />
        <Route path="/create-agent" element={<CreateAgentPage />} />
        <Route path="/new-app/draft" element={<DraftAppPage />} />
        <Route path="/agents" element={<AgentsPage />} />
        <Route path="/agents/:subscriptionId" element={<AgentsPage />} />
        <Route path="/agents/:subscriptionId/:app/:name" element={<AgentDetailPage />} />
        <Route path="/apps/:subscriptionId/:app" element={<AppDetailPage />} />
        <Route path="/playground" element={<PlaygroundPage />} />
        <Route path="/playground/:subscriptionId/:app/:name" element={<PlaygroundPage />} />
        <Route path="*" element={<Navigate to="/agents" replace />} />
      </Routes>
    </Shell>
  )
}
