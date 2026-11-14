import { KillSwitchBar } from './components/kill-switch'
import { StatusBar } from './components/status-bar'
import { LiveUpdatesProvider } from './hooks/live-updates-provider'
import { useStatusBar } from './hooks/use-status-bar'
import { resolveRoute } from './routes/route-match'
import { OpportunityDetailPage } from './routes/opportunity-detail'
import { OpportunitiesPage } from './routes/opportunities'
import { JournalDetailPage } from './routes/journal-detail'
import { JournalPage } from './routes/journal'
import { PaperPage } from './routes/paper'
import { PositionDetailPage } from './routes/position-detail'
import { PositionsPage } from './routes/positions'
import { ReportDetailPage } from './routes/report-detail'
import { ReportsPage } from './routes/reports'
import { ScanDetailPage } from './routes/scan-detail'
import { ScansPage } from './routes/scans'
import { SymbolInspectorPage } from './routes/symbol-inspector'
import { SystemPage } from './routes/system'
import { TriggerDetailPage } from './routes/trigger-detail'
import { TriggersPage } from './routes/triggers'
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
    <LiveUpdatesProvider>
      <div className="app-shell">
        <StatusBar {...statusBar} />
        <KillSwitchBar />
        <main className="app-main">
          {route.inspectorSymbol !== null ? (
            <SymbolInspectorPage key={route.inspectorSymbol} symbol={route.inspectorSymbol} />
          ) : route.scanId !== null ? (
            <ScanDetailPage key={route.scanId} scanId={route.scanId} />
          ) : route.reportId !== null ? (
            <ReportDetailPage key={route.reportId} reportId={route.reportId} />
          ) : route.opportunityId !== null ? (
            <OpportunityDetailPage key={route.opportunityId} opportunityId={route.opportunityId} />
          ) : route.triggerId !== null ? (
            <TriggerDetailPage key={route.triggerId} triggerId={route.triggerId} />
          ) : route.positionId !== null ? (
            <PositionDetailPage key={route.positionId} positionId={route.positionId} />
          ) : route.journalTradeId !== null ? (
            <JournalDetailPage key={route.journalTradeId} tradeId={route.journalTradeId} />
          ) : route.isScans ? (
            <ScansPage />
          ) : route.isReports ? (
            <ReportsPage />
          ) : route.isOpportunities ? (
            <OpportunitiesPage />
          ) : route.isTriggers ? (
            <TriggersPage />
          ) : route.isPositions ? (
            <PositionsPage />
          ) : route.isJournal ? (
            <JournalPage />
          ) : route.isPaper ? (
            <PaperPage />
          ) : route.isWatchlist ? (
            <WatchlistPage />
          ) : route.isSystem ? (
            <SystemPage />
          ) : (
            <section className="system-page">
              <h1>Not found</h1>
              <p className="status-message">
                Open <code>/watchlist</code>, <code>/scans</code>, <code>/reports</code>,{' '}
                <code>/opportunities</code>, <code>/triggers</code>, <code>/positions</code>,{' '}
                <code>/journal</code>, <code>/paper</code>, or <code>/system</code>.
              </p>
            </section>
          )}
        </main>
        <footer className="app-footer">
          Symbol registry · fixture-backed · metrics-only CALC reports · no exchange, LLM, or
          credential connection · read-only at 375px
        </footer>
      </div>
    </LiveUpdatesProvider>
  )
}
