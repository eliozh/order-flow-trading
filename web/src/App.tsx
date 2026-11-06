import { StatusBar } from './components/status-bar'
import { useStatusBar } from './hooks/use-status-bar'
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

function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment)
  } catch {
    return segment
  }
}

export default function App() {
  const path = currentPath()
  const isSystem = path === '/' || path.startsWith('/system')
  const symbolMatch = /^\/watchlist\/([^/]+)\/?$/.exec(path)
  const isWatchlistSearch = path === '/watchlist/search' || path === '/watchlist/'
  const inspectorSymbol =
    symbolMatch !== null && !isWatchlistSearch ? decodeSegment(symbolMatch[1]) : null
  const isWatchlist = path === '/watchlist' || path.startsWith('/watchlist/')
  const statusBar = useStatusBar()

  return (
    <div className="app-shell">
      <StatusBar {...statusBar} />
      <main className="app-main">
        {inspectorSymbol !== null ? (
          <SymbolInspectorPage symbol={inspectorSymbol} />
        ) : isWatchlist ? (
          <WatchlistPage />
        ) : isSystem ? (
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
