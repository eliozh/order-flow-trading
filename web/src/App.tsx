import { StatusBar } from './components/status-bar'
import { useStatusBar } from './hooks/use-status-bar'
import { SystemPage } from './routes/system'
import './styles/tokens.css'
import './styles/app.css'

function currentPath(): string {
  if (typeof window === 'undefined') {
    return '/system'
  }
  return window.location.pathname
}

export default function App() {
  const path = currentPath()
  const isSystem = path === '/' || path.startsWith('/system')
  const statusBar = useStatusBar()

  return (
    <div className="app-shell">
      <StatusBar {...statusBar} />
      <main className="app-main">
        {isSystem ? (
          <SystemPage />
        ) : (
          <section className="system-page">
            <h1>Not found</h1>
            <p className="status-message">
              This route is not part of the walking skeleton. Open <code>/system</code>.
            </p>
          </section>
        )}
      </main>
      <footer className="app-footer">
        Walking skeleton · fixture-backed · no exchange connection · read-only at 375px
      </footer>
    </div>
  )
}
