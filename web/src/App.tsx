import { StatusBar } from './components/status-bar'
import { useStatusBar } from './hooks/use-status-bar'
import { resolveRoute } from './routes/route-match'
import { OpportunityDetailPage } from './routes/opportunity-detail'
import { OpportunitiesPage } from './routes/opportunities'
import { ReportDetailPage } from './routes/report-detail'
import { ReportsPage } from './routes/reports'
import { ScanDetailPage } from './routes/scan-detail'
import { ScansPage } from './routes/scans'
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
        ) : route.scanId !== null ? (
          <ScanDetailPage key={route.scanId} scanId={route.scanId} />
        ) : route.reportId !== null ? (
          <ReportDetailPage key={route.reportId} reportId={route.reportId} />
        ) : route.opportunityId !== null ? (
          <OpportunityDetailPage key={route.opportunityId} opportunityId={route.opportunityId} />
        ) : route.isScans ? (
          <ScansPage />
        ) : route.isReports ? (
          <ReportsPage />
        ) : route.isOpportunities ? (
          <OpportunitiesPage />
        ) : route.isWatchlist ? (
          <WatchlistPage />
        ) : route.isSystem ? (
          <SystemPage />
        ) : (
          <section className="system-page">
            <h1>Not found</h1>
            <p className="status-message">
              Open <code>/watchlist</code>, <code>/scans</code>, <code>/reports</code>,{' '}
              <code>/opportunities</code>, or <code>/system</code>.
            </p>
          </section>
        )}
      </main>
      <footer className="app-footer">
        Symbol registry · fixture-backed · metrics-only CALC reports · no exchange, LLM, or
        credential connection · read-only at 375px
      </footer>
    </div>
  )
}
