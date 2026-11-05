import { StatusPanel } from './components/StatusPanel'
import './styles/tokens.css'
import './styles/app.css'

export default function App() {
  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">OFT</div>
        <div className="header-meta">
          <span className="env-chip env-chip--paper">PAPER</span>
          <span className="muted">order-flow terminal</span>
        </div>
      </header>
      <main className="app-main">
        <StatusPanel />
      </main>
      <footer className="app-footer">
        Walking skeleton · fixture-backed · no exchange connection
      </footer>
    </div>
  )
}
