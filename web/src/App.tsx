import { StatusBar } from './components/status-bar'
import { useStatusBar } from './hooks/use-status-bar'
import { resolveRoute } from './routes/route-match'
import { SymbolInspectorPage } from './routes/symbol-inspector'
import { SystemPage } from './routes/system'
import { WatchlistPage } from './routes/watchlist'
import './styles/tokens.css'
import './styles/app.css'

function currentPath(): string {
  if (typeof window === 'undefined') {
    return '/system'
  }
  return window.location.pathname
}

export default function App() {
  const route = resolveRoute(currentPath())
  const statusBar = useStatusBar()

  return (
    <div className="app-shell">
      <StatusBar {...statusBar} />
      <main className="app-main">
        {route.inspectorSymbol !== null ? (
          <SymbolInspectorPage key={route.inspectorSymbol} symbol={route.inspectorSymbol} />
        ) : route.isWatchlist ? (
          <WatchlistPage />
        ) : route.isSystem ? (
          <SystemPage />
        ) : (
          <section className="system-page">
            <h1>Not found</h1>
            <p className="status-message">
              This route is not part of the symbol slice. Open <code>/watchlist</code> or{' '}
              <code>/system</code>.
            </p>
          </section>
        )}
      </main>
      <footer className="app-footer">
        Symbol registry · fixture-backed · no exchange connection · read-only at 375px
      </footer>
    </div>
  )
}
