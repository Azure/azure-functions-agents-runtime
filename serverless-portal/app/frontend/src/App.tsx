import { Routes, Route } from 'react-router-dom'
import Shell from './components/Shell'
import AgentsPage from './pages/AgentsPage'
import CreateAgentPage from './pages/CreateAgentPage'
import EditAgentPage from './pages/EditAgentPage'

export default function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<AgentsPage />} />
        <Route path="/create" element={<CreateAgentPage />} />
        <Route path="/edit/:name" element={<EditAgentPage />} />
      </Routes>
    </Shell>
  )
}
