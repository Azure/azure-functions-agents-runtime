import { Routes, Route } from 'react-router-dom'
import Shell from './components/Shell'
import AgentsPage from './pages/AgentsPage'

export default function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<AgentsPage />} />
      </Routes>
    </Shell>
  )
}
