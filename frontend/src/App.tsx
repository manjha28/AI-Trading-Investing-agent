import { Activity, ArrowDownLeft, Bitcoin, TrendingUp, BookOpen, CircleAlert, Landmark, LogOut, PanelLeftClose, PanelLeftOpen, Search, ShieldCheck, Target, Wallet } from 'lucide-react';
import { Suspense, lazy, useEffect, useState } from 'react';
import { apiRequest } from './api';
import { marketStatusLabel } from './lib/priceStates';
import { useExecutionState } from './hooks/useExecutionState';
import { useWorkspace } from './components/useWorkspace';
import { AlertsBell } from './components/AlertsBell';
import { MobileNav } from './components/MobileNav';
import { CodeInput } from './components/ui/kit';
import { DhanStatusRow } from './components/DhanStatusRow';
import { StatusPill } from './components/StatusPill';
import { SlowBackendBanner } from './components/SlowBackendBanner';
import { TopLoadingBar } from './components/TopLoadingBar';
import { BrandLockup } from './components/BrandLockup';
import { AuthPage } from './pages/Auth';
import { BrokersPage } from './pages/BrokersPage';
const CryptoDeltaPage = lazy(() => import('./pages/CryptoDelta').then((m) => ({ default: m.CryptoDeltaPage })));
import { JournalPage } from './pages/Journal';
const SignalsWorkspace = lazy(() => import('./signals/SignalsWorkspace').then((m) => ({ default: m.SignalsWorkspace })));
const MarketDataPage = lazy(() => import('./pages/MarketData').then((m) => ({ default: m.MarketDataPage })));
import type { WatchFocus } from './pages/MarketWatch';
const MarketWatch = lazy(() => import('./pages/MarketWatch').then((m) => ({ default: m.MarketWatch })));
import { OverviewPage } from './pages/Overview';
import { PortfolioSetupPage } from './pages/PortfolioSetup';
const Research = lazy(() => import('./pages/Research').then((m) => ({ default: m.Research })));
const ForwardRecordPage = lazy(() => import('./pages/ForwardRecord').then((m) => ({ default: m.ForwardRecordPage })));

export default function App() {
  const workspace = useWorkspace();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [hasConnectedBroker, setHasConnectedBroker] = useState(false);
  const [brokerStatusChecked, setBrokerStatusChecked] = useState(false);
  const [headerSyncing, setHeaderSyncing] = useState(false);
  const [headerMenuOpen, setHeaderMenuOpen] = useState(false);
  const {
    user,
    checkingSession,
    activeView,
    setActiveView,
    marketQuery,
    setMarketQuery,
    searchMarketInstruments,
    requestBrokerRefresh,
    portfolios,
    portfolioId,
    setPortfolioId,
    error,
    setError,
    notice,
    setNotice,
    marketDataStatus,
    onboardingDismissed,
    setOnboardingDismissed,
    twoFactorStatus,
    twoFactorSecret,
    twoFactorProvisioningUri,
    twoFactorCode,
    setTwoFactorCode,
    twoFactorBusy,
    twoFactorError,
    beginTwoFactorSetup,
    verifyTwoFactor,
    resendVerificationEmail,
    signOut,
  } = workspace;

  // Alerts live in the header bell and on each stock in Market watch. Clicking one opens that stock there.
  const [watchFocus, setWatchFocus] = useState<WatchFocus | undefined>(undefined);
  const [bellKey, setBellKey] = useState(0);
  const [researchFocus, setResearchFocus] = useState<{ instrumentId: string; nonce: number } | null>(null);
  const openStock = (instrumentId: string, tab: WatchFocus['tab'] = 'alerts') => { setWatchFocus((cur) => ({ instrumentId, tab, nonce: (cur?.nonce ?? 0) + 1 })); setActiveView('market-watch'); };

  // What the sidebar and footer say about live orders is the backend's effective state per venue (OFF, ARMED BUT BLOCKED, LIVE), read once the session is verified.
  const sessionVerified = !!twoFactorStatus?.session_verified && !!user?.email_verified;
  const executionStates = useExecutionState(sessionVerified);

  // The header search opens the Market watch with that term (n makes repeating a search re-run it).
  const [watchSearch, setWatchSearch] = useState({ q: '', n: 0 });

  // Success notices are transient; errors stay until dismissed.
  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(''), 8000);
    return () => window.clearTimeout(timer);
  }, [notice, setNotice]);

  useEffect(() => {
    const userId = user?.id;
    if (!userId) {
      setHasConnectedBroker(false);
      setBrokerStatusChecked(false);
      return;
    }
    let active = true;
    setBrokerStatusChecked(false);
    void apiRequest<{ connected: boolean }>('/brokers/connection-status')
      .then((status) => {
        if (active) {
          setHasConnectedBroker(status.connected);
          setBrokerStatusChecked(true);
        }
      })
      .catch(() => {
        if (active) {
          setHasConnectedBroker(false);
          setBrokerStatusChecked(true);
        }
      });
    return () => {
      active = false;
    };
  }, [user?.id, activeView]);

  async function syncBroker() {
    setHeaderSyncing(true);
    setError('');
    try {
      const result = await apiRequest<{
        sync_status: string;
        endpoint_results: Record<string, { status: string; record_count?: string; skipped_count?: string; reason?: string }>;
        holdings_count: number;
        trades_count: number | null;
        ledger_count: number | null;
      }>('/brokers/dhan/sync', { method: 'POST' });
      const skippedLedger = Number(result.endpoint_results.ledger?.skipped_count ?? 0);
      const failedEndpoints = Object.entries(result.endpoint_results)
        .filter(([, endpoint]) => endpoint.status === 'failed')
        .map(([name]) => name);
      const skippedText = skippedLedger > 0
        ? `; skipped ${skippedLedger} ledger entr${skippedLedger === 1 ? 'y' : 'ies'} (missing provider IDs)`
        : '; skipped 0 ledger entries';
      const failureText = failedEndpoints.length ? `; failed endpoints: ${failedEndpoints.join(', ')}` : '';
      const closeRefreshText = result.endpoint_results.official_close_refresh?.status === 'failed'
        ? ' Official close could not be re-fetched; the previous close and retrieval timestamp were retained.'
        : '';
      setNotice(`Dhan sync ${result.sync_status}: holdings updated ${result.holdings_count}; trades imported ${result.trades_count ?? 0}; ledger entries processed ${result.ledger_count ?? 0}${skippedText}${failureText}.${closeRefreshText}`);
      requestBrokerRefresh();
    } catch (syncError) {
      setError(syncError instanceof Error ? syncError.message : 'Dhan account sync failed.');
    } finally {
      setHeaderSyncing(false);
    }
  }

  if (workspace.verificationToken) {
    return <AuthPage workspace={{error: workspace.error, registering: workspace.registering, setError: workspace.setError, setRegistering: workspace.setRegistering, submitAuth: workspace.submitAuth, verificationStatus: workspace.verificationStatus, verificationToken: workspace.verificationToken}} />;
  }

  if (checkingSession) {
    return <main className="loading-screen"><Activity size={20} /> Checking secure session</main>;
  }

  if (!user) {
    return <AuthPage workspace={{error: workspace.error, registering: workspace.registering, setError: workspace.setError, setRegistering: workspace.setRegistering, submitAuth: workspace.submitAuth, verificationStatus: workspace.verificationStatus, verificationToken: workspace.verificationToken}} />;
  }

  const twoFactorPanel = (
    <section className="data-note" aria-label="Two-factor authentication">
      <div>
        <strong>{twoFactorStatus?.enabled ? 'Verify this session' : 'Set up two-factor authentication'}</strong>
        <span>
          {twoFactorStatus?.enabled
            ? ' Enter the current six-digit code from your authenticator app to unlock broker access.'
            : ' Broker connections require app-based two-factor authentication. Use an authenticator app; FinByte Nexus never asks for your broker PIN or OTP.'}
        </span>
        {twoFactorSecret && (
          <div className="form-stack">
            <p>Scan the provisioning link with an authenticator app, or enter this setup key manually:</p>
            <a href={twoFactorProvisioningUri}>Open authenticator app</a>
            <code>{twoFactorSecret}</code>
          </div>
        )}
        {!twoFactorStatus?.enabled && !twoFactorSecret && (
          <button className="secondary-button" type="button" onClick={() => void beginTwoFactorSetup()} disabled={twoFactorBusy}>
            {twoFactorBusy ? 'Preparing…' : 'Set up authenticator app'}
          </button>
        )}
        {(twoFactorStatus?.enabled || twoFactorSecret) && (
          <form className="form-stack" onSubmit={(event) => void verifyTwoFactor(event)}>
            <label>
              Authenticator code
              <CodeInput value={twoFactorCode} onChange={setTwoFactorCode} />
            </label>
            <button className="primary-button" type="submit" disabled={twoFactorBusy || twoFactorCode.length !== 6}>
              {twoFactorBusy ? 'Verifying…' : twoFactorStatus?.enabled ? 'Verify session' : 'Verify and enable'}
            </button>
          </form>
        )}
        {twoFactorError && <p className="error-message" role="alert">{twoFactorError}</p>}
      </div>
    </section>
  );

  const pageTitle = activeView === 'overview'
    ? 'Overview'
    : activeView === 'journal'
      ? 'Trade journal'
      : activeView === 'market-data'
        ? 'Instrument details'
        : activeView === 'brokers'
          ? 'Dhan connection'
          : activeView === 'research'
                  ? 'Research'
                  : activeView === 'signals'
                    ? 'Signals'
                    : activeView === 'crypto-delta'
                      ? 'Crypto · Delta'
                      : activeView === 'market-watch'
                        ? 'Market watch'
                        : 'Overview';

  return (
    <div className={sidebarCollapsed ? 'app-frame sidebar-collapsed' : 'app-frame'}>
      <aside className="sidebar">
        <BrandLockup />
        <button
          className="sidebar-collapse"
          type="button"
          onClick={() => setSidebarCollapsed((collapsed) => !collapsed)}
          aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-expanded={!sidebarCollapsed}
          aria-controls="primary-navigation"
        >
          {sidebarCollapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
          <span>{sidebarCollapsed ? 'Expand' : 'Collapse'}</span>
        </button>
        <p className="nav-label">WORKSPACE</p>
        <nav className="primary-nav" id="primary-navigation" aria-label="Workspace">
          <button className={activeView === 'overview' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('overview')}><Wallet size={17} /><span>Overview</span></button>
          <button className={activeView === 'journal' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('journal')}><Activity size={17} /><span>Trade journal</span></button>
          <button className={activeView === 'market-watch' || activeView === 'market-data' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('market-watch')}><TrendingUp size={17} /><span>Market watch</span></button>
          <button className={activeView === 'research' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('research')}><BookOpen size={17} /><span>Research</span></button>
          <button className={activeView === 'signals' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('signals')}><Target size={17} /><span>Signals</span></button>
          <button className={activeView === 'crypto-delta' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('crypto-delta')}><Bitcoin size={17} /><span>Crypto · Delta</span></button>
        </nav>
        <DhanStatusRow allowed={sessionVerified} onOpen={() => setActiveView('brokers')} />
        <p className="sidebar-user">{user.email} · {user.email_verified ? 'Email verified' : 'Email unverified'}</p>
        <button className="signout-button" onClick={signOut}><LogOut size={16} /><span>Sign out</span></button>
      </aside>

      <main className="workspace">
        <TopLoadingBar />
        <SlowBackendBanner />
        <header className="workspace-header">
          <form className="header-search" role="search" onSubmit={(event) => { event.preventDefault(); setWatchSearch((current) => ({ q: marketQuery.trim(), n: current.n + 1 })); setActiveView('market-watch'); }}>
            <Search size={15} aria-hidden="true" />
            <input type="search" aria-label="Search instruments" placeholder="Search instruments" value={marketQuery} onChange={(event) => setMarketQuery(event.target.value)} />
          </form>
          <div className="header-actions">
            <button className="header-menu-button" type="button" aria-expanded={headerMenuOpen} aria-label="Menu" onClick={() => setHeaderMenuOpen((open) => !open)}>☰</button>
            <AlertsBell refreshKey={bellKey} onOpenStock={(id) => openStock(id)} onOpenResearch={(id) => { setResearchFocus((cur) => ({ instrumentId: id, nonce: (cur?.nonce ?? 0) + 1 })); setActiveView('research'); }} />
            <div className={headerMenuOpen ? "header-menu open" : "header-menu"}>
            {portfolios.length > 0 && <label className="portfolio-picker"><span>Portfolio</span><select value={portfolioId} onChange={(event) => setPortfolioId(event.target.value)}>{portfolios.map((portfolio) => <option key={portfolio.id} value={portfolio.id}>{portfolio.name}</option>)}</select></label>}
            <StatusPill states={executionStates} marketLabel={marketStatusLabel(marketDataStatus)} marketStatus={marketDataStatus} canSync={hasConnectedBroker} syncing={headerSyncing} onSync={() => void syncBroker()} onOpenBrokers={() => setActiveView('brokers')} />
            </div>
          </div>
        </header>
        {activeView !== 'overview' && <h1 className="page-title">{pageTitle}</h1>}

        {error && <p className="error-banner" role="alert"><CircleAlert size={17} />{error}<button type="button" onClick={() => setError('')} aria-label="Dismiss error">×</button></p>}
        {notice && <p className="success-banner" role="status"><ShieldCheck size={17} />{notice}<button type="button" onClick={() => setNotice('')} aria-label="Dismiss notice">×</button></p>}

        {(activeView === 'overview' || activeView === 'brokers') && !onboardingDismissed && brokerStatusChecked && !hasConnectedBroker && (
          <section className="data-note" aria-label="Broker onboarding">
            <div>
              <strong>Connect your broker account.</strong>
              <span>
                {user.email_verified
                  ? ' Complete app-based two-factor verification, then use a broker’s official sign-in to securely read supported account data. You can also skip and use manual entry and CSV import.'
                  : ' Verify your email before connecting a broker. You can continue using manual entry and CSV import.'}
              </span>
            </div>
            {!user.email_verified && <button className="secondary-button" type="button" onClick={() => void resendVerificationEmail()}>Resend verification email</button>}
            {user.email_verified && twoFactorStatus && !twoFactorStatus.session_verified && activeView !== 'brokers' && twoFactorPanel}
            <button className="primary-button" type="button" disabled={!user.email_verified || !twoFactorStatus?.session_verified} onClick={() => setActiveView('brokers')}>Connect broker</button>
            <button className="text-button" type="button" onClick={() => setOnboardingDismissed(true)}>Skip for now</button>
          </section>
        )}

        <Suspense fallback={<p className="ui-muted" role="status" aria-busy="true">Loading…</p>}>
        {typeof window !== 'undefined' && window.location.hash === '#forward-record' ? (
          <ForwardRecordPage />
        ) : activeView === 'research' ? (
          <Research focus={researchFocus} watchlist={workspace.watchlist} addInstrumentToWatchlist={workspace.addInstrumentToWatchlist} removeInstrumentFromWatchlist={workspace.removeInstrumentFromWatchlist} />
        ) : activeView === 'signals' ? (
          <SignalsWorkspace />
        ) : activeView === 'crypto-delta' ? (
          <CryptoDeltaPage />
        ) : activeView === 'market-watch' ? (
          <MarketWatch
            initialQuery={watchSearch.q}
            searchNonce={watchSearch.n}
            focus={watchFocus}
            onAlertsChanged={() => setBellKey((k) => k + 1)}
            workspace={{
              watchlist: workspace.watchlist,
              addInstrumentToWatchlist: workspace.addInstrumentToWatchlist,
              removeInstrumentFromWatchlist: workspace.removeInstrumentFromWatchlist,
              setActiveView: workspace.setActiveView,
              setOpenMarketInstrumentId: workspace.setOpenMarketInstrumentId,
            }}
          />
        ) : activeView === 'brokers' ? (
          <>
            {user.email_verified && twoFactorStatus && !twoFactorStatus.session_verified && twoFactorPanel}
            <button className="secondary-button" type="button" onClick={() => setActiveView('overview')} style={{ marginBottom: 12 }}>← Back to Overview</button>
            <BrokersPage portfolioId={portfolioId} />
          </>
        ) : activeView === 'market-data' ? (
          <>
          <button className="secondary-button" type="button" onClick={() => setActiveView('market-watch')} style={{ marginBottom: 12 }}>← Market watch</button>
          <MarketDataPage userId={user.id} detailsOnly workspace={{
            attachSelectedInstrument: workspace.attachSelectedInstrument,
            currency: workspace.currency,
            hasOlderMarketHistory: workspace.hasOlderMarketHistory,
            loadInstrumentIndicators: workspace.loadInstrumentIndicators,
            loadMarketHistory: workspace.loadMarketHistory,
            loadMarketQuote: workspace.loadMarketQuote,
            loadOlderMarketHistory: workspace.loadOlderMarketHistory,
            loadingMarketHistory: workspace.loadingMarketHistory,
            loadingOlderMarketHistory: workspace.loadingOlderMarketHistory,
            loadingMarketQuote: workspace.loadingMarketQuote,
            marketEnd: workspace.marketEnd,
            marketHistory: workspace.marketHistory,
            marketIndicators: workspace.marketIndicators,
            marketInstruments: workspace.marketInstruments,
            marketInterval: workspace.marketInterval,
            marketQuery: workspace.marketQuery,
            marketQuote: workspace.marketQuote,
            marketStart: workspace.marketStart,
            openMarketInstrumentId: workspace.openMarketInstrumentId,
            setOpenMarketInstrumentId: workspace.setOpenMarketInstrumentId,
            searchMarketInstruments: workspace.searchMarketInstruments,
            searchingMarket: workspace.searchingMarket,
            selectMarketInstrument: workspace.selectMarketInstrument,
            selectedInstrument: workspace.selectedInstrument,
            setMarketEnd: workspace.setMarketEnd,
            setMarketHistory: workspace.setMarketHistory,
            setMarketInterval: workspace.setMarketInterval,
            setMarketQuery: workspace.setMarketQuery,
            setMarketStart: workspace.setMarketStart,
            summary: workspace.summary,
            valuation: workspace.valuation,
          }} />
          </>
        ) : activeView === 'journal' ? (
          <JournalPage workspace={{cancelEditingTrade: workspace.cancelEditingTrade, currency: workspace.currency, editingTrade: workspace.editingTrade, journalAnalytics: workspace.journalAnalytics, journalAssetClass: workspace.journalAssetClass, journalBreakdown: workspace.journalBreakdown, journalCurrency: workspace.journalCurrency, journalHasExit: workspace.journalHasExit, journalTrades: workspace.journalTrades, journalLoadState: workspace.journalLoadState, journalLoadError: workspace.journalLoadError, removeJournalTrade: workspace.removeJournalTrade, refreshJournalTrades: workspace.refreshJournalTrades, saveJournalNotes: workspace.saveJournalNotes, setJournalAssetClass: workspace.setJournalAssetClass, setJournalCurrency: workspace.setJournalCurrency, setJournalHasExit: workspace.setJournalHasExit, startEditingTrade: workspace.startEditingTrade, submitJournalTrade: workspace.submitJournalTrade, valuation: workspace.valuation}} />
        ) : portfolios.length === 0 ? (
          <PortfolioSetupPage workspace={{createPortfolio: workspace.createPortfolio}} />
        ) : activeView === 'overview' ? (
          <OverviewPage brokerAccessAllowed={!!twoFactorStatus?.session_verified} workspace={{addInstrumentToWatchlist: workspace.addInstrumentToWatchlist, cancelEditingTransaction: workspace.cancelEditingTransaction, coverage: workspace.coverage, csvContent: workspace.csvContent, csvPreview: workspace.csvPreview, currency: workspace.currency, editingTransaction: workspace.editingTransaction, exportTransactionCsv: workspace.exportTransactionCsv, importTransactionCsv: workspace.importTransactionCsv, importingCsv: workspace.importingCsv, loadTransactionCsv: workspace.loadTransactionCsv, loadingPortfolio: workspace.loadingPortfolio, marketDataStatus: workspace.marketDataStatus, portfolioId: workspace.portfolioId, previewTransactionCsv: workspace.previewTransactionCsv, refreshPortfolioValuation: workspace.refreshPortfolioValuation, refreshingValuation: workspace.refreshingValuation, removeInstrumentFromWatchlist: workspace.removeInstrumentFromWatchlist, riskMetrics: workspace.riskMetrics, setActiveView: workspace.setActiveView, setOpenMarketInstrumentId: workspace.setOpenMarketInstrumentId, setCoverageVersion: workspace.setCoverageVersion, setThesisSymbol: workspace.setThesisSymbol, setTransactionType: workspace.setTransactionType, submitTransaction: workspace.submitTransaction, summary: workspace.summary, thesisSymbol: workspace.thesisSymbol, transactionType: workspace.transactionType, valuation: workspace.valuation, valuationBySymbol: workspace.valuationBySymbol, watchlist: workspace.watchlist, watchlistLoadState: workspace.watchlistLoadState, watchlistLoadError: workspace.watchlistLoadError, refreshWatchlist: workspace.refreshWatchlist, setBrokerPriceRows: workspace.setBrokerPriceRows, brokerRefreshKey: workspace.brokerRefreshKey}} />
        ) : null}
        <footer className="workspace-footer"><span><ArrowDownLeft size={14} /> Decision support, not investment advice.</span><span>Data entered by you · {portfolios.length} portfolio{portfolios.length === 1 ? '' : 's'}</span></footer>
        </Suspense>
      </main>
      <MobileNav activeView={activeView} setActiveView={setActiveView} onSignOut={signOut} />
    </div>
  );
}
