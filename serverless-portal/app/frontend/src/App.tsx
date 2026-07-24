import { Routes, Route, Navigate } from 'react-router-dom'
import Shell from './components/Shell'
import AgentsPage from './pages/AgentsPage'
import AgentDetailPage from './pages/AgentDetailPage'

export default function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Navigate to="/agents" replace />} />
        <Route path="/agents" element={<AgentsPage />} />
        <Route path="/agents/:subscriptionId" element={<AgentsPage />} />
        <Route path="/agents/:subscriptionId/:app/:name" element={<AgentDetailPage />} />
        <Route path="*" element={<Navigate to="/agents" replace />} />
      </Routes>
    </Shell>
  )
}
