import { Routes, Route, Navigate } from 'react-router-dom'
import Shell from './components/Shell'
import AgentsPage from './pages/AgentsPage'
import AgentDetailPage from './pages/AgentDetailPage'
import WorkflowsPage from './pages/WorkflowsPage'
import WorkflowBuilderPage from './pages/WorkflowBuilderPage'
import WorkflowRunPage from './pages/WorkflowRunPage'

export default function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Navigate to="/workflows" replace />} />
        <Route path="/workflows" element={<WorkflowsPage />} />
        <Route path="/workflows/:id" element={<WorkflowBuilderPage />} />
        <Route path="/workflows/:id/run" element={<WorkflowRunPage />} />
        <Route path="/agents" element={<AgentsPage />} />
        <Route path="/agents/:subscriptionId" element={<AgentsPage />} />
        <Route path="/agents/:subscriptionId/:app/:name" element={<AgentDetailPage />} />
        <Route path="*" element={<Navigate to="/workflows" replace />} />
      </Routes>
    </Shell>
  )
}
