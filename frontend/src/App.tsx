import { Activity, ArrowDownLeft, BarChart3, BellRing, BookOpen, CalendarDays, CircleAlert, CircleDollarSign, Landmark, LogOut, PanelLeftClose, PanelLeftOpen, ReceiptText, ShieldCheck, Wallet } from 'lucide-react';
import { useState } from 'react';
import { useWorkspace } from './components/useWorkspace';
import { ActivityPage } from './pages/Activity';
import { Alerts } from './pages/Alerts';
import { AuthPage } from './pages/Auth';
import { BrokersPage } from './pages/BrokersPage';
import { CorporateActions } from './pages/CorporateActions';
import { JournalPage } from './pages/Journal';
import { MarketDataPage } from './pages/MarketData';
import { OverviewPage } from './pages/Overview';
import { PortfolioSetupPage } from './pages/PortfolioSetup';
import { Research } from './pages/Research';

export default function App() {
  const workspace = useWorkspace();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const {
    user,
    checkingSession,
    activeView,
    setActiveView,
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
            ? ' Enter the current six-digit code from your authenticator app to unlock read-only broker access.'
            : ' Broker connections require app-based two-factor authentication. Use an authenticator app; Trading Copilot never asks for your broker PIN or OTP.'}
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
              <input
                name="two_factor_code"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                maxLength={6}
                value={twoFactorCode}
                onChange={(event) => setTwoFactorCode(event.target.value)}
                required
              />
            </label>
            <button className="primary-button" type="submit" disabled={twoFactorBusy}>
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
        ? 'Market data'
        : activeView === 'brokers'
          ? 'Connect Broker'
          : activeView === 'alerts'
            ? 'Alerts'
              : activeView === 'corporate-actions'
                ? 'Corporate actions'
                : activeView === 'research'
                  ? 'Research'
              : 'Transactions';

  return (
    <div className={sidebarCollapsed ? 'app-frame sidebar-collapsed' : 'app-frame'}>
      <aside className="sidebar">
        <div className="brand-lockup"><span className="brand-symbol"><Activity size={20} /></span><span>Trading Copilot</span></div>
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
          <button className={activeView === 'activity' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('activity')}><ReceiptText size={17} /><span>Transactions</span></button>
          <button className={activeView === 'journal' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('journal')}><Activity size={17} /><span>Trade journal</span></button>
          <button className={activeView === 'market-data' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('market-data')}><BarChart3 size={17} /><span>Market data</span></button>
          <button className={activeView === 'brokers' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('brokers')}><Landmark size={17} /><span>Brokers</span></button>
          <button className={activeView === 'alerts' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('alerts')}><BellRing size={17} /><span>Alerts</span></button>
          <button className={activeView === 'corporate-actions' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('corporate-actions')}><CalendarDays size={17} /><span>Corporate actions</span></button>
          <button className={activeView === 'research' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('research')}><BookOpen size={17} /><span>Research</span></button>
        </nav>
        <div className="sidebar-status"><span className={`status-dot ${marketDataStatus}`} /><span>Paper ledger only</span><span>Market data: {marketDataStatus}</span></div>
        <button className="signout-button" onClick={signOut}><LogOut size={16} /><span>Sign out</span></button>
      </aside>

      <main className="workspace">
        <header className="workspace-header">
          <div><p className="eyebrow">PORTFOLIO WORKSPACE</p><h1>{pageTitle}</h1></div>
          <div className="header-actions">
            {portfolios.length > 0 && <label className="portfolio-picker"><span>Portfolio</span><select value={portfolioId} onChange={(event) => setPortfolioId(event.target.value)}>{portfolios.map((portfolio) => <option key={portfolio.id} value={portfolio.id}>{portfolio.name}</option>)}</select></label>}
            <span className={`market-status market-status-${marketDataStatus}`} role="status">Market data: {marketDataStatus}</span>
            <span className="user-chip">{user.email} · {user.email_verified ? 'Email verified' : 'Email unverified'}</span>
          </div>
        </header>

        {error && <p className="error-banner" role="alert"><CircleAlert size={17} />{error}<button type="button" onClick={() => setError('')} aria-label="Dismiss error">×</button></p>}
        {notice && <p className="success-banner" role="status"><ShieldCheck size={17} />{notice}<button type="button" onClick={() => setNotice('')} aria-label="Dismiss notice">×</button></p>}

        {!onboardingDismissed && (
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

        {activeView === 'corporate-actions' ? (
          <CorporateActions portfolioId={portfolioId} watchlist={workspace.watchlist} addInstrumentToWatchlist={workspace.addInstrumentToWatchlist} removeInstrumentFromWatchlist={workspace.removeInstrumentFromWatchlist} />
        ) : activeView === 'research' ? (
          <Research watchlist={workspace.watchlist} addInstrumentToWatchlist={workspace.addInstrumentToWatchlist} removeInstrumentFromWatchlist={workspace.removeInstrumentFromWatchlist} />
        ) : activeView === 'alerts' ? (
          <Alerts portfolios={portfolios} portfolioId={portfolioId} />
        ) : activeView === 'brokers' ? (
          <>
            {user.email_verified && twoFactorStatus && !twoFactorStatus.session_verified && twoFactorPanel}
            <BrokersPage portfolioId={portfolioId} />
          </>
        ) : activeView === 'market-data' ? (
          <MarketDataPage workspace={{addInstrumentToWatchlist: workspace.addInstrumentToWatchlist, addSelectedInstrumentToWatchlist: workspace.addSelectedInstrumentToWatchlist, attachSelectedInstrument: workspace.attachSelectedInstrument, currency: workspace.currency, loadInstrumentIndicators: workspace.loadInstrumentIndicators, loadMarketHistory: workspace.loadMarketHistory, loadMarketQuote: workspace.loadMarketQuote, loadingMarketHistory: workspace.loadingMarketHistory, loadingMarketIndicators: workspace.loadingMarketIndicators, loadingMarketQuote: workspace.loadingMarketQuote, marketEnd: workspace.marketEnd, marketHistory: workspace.marketHistory, marketIndicators: workspace.marketIndicators, marketInstruments: workspace.marketInstruments, marketInterval: workspace.marketInterval, marketQuery: workspace.marketQuery, marketQuote: workspace.marketQuote, marketStart: workspace.marketStart, removeInstrumentFromWatchlist: workspace.removeInstrumentFromWatchlist, searchMarketInstruments: workspace.searchMarketInstruments, searchingMarket: workspace.searchingMarket, selectMarketInstrument: workspace.selectMarketInstrument, selectedInstrument: workspace.selectedInstrument, setMarketEnd: workspace.setMarketEnd, setMarketHistory: workspace.setMarketHistory, setMarketInterval: workspace.setMarketInterval, setMarketQuery: workspace.setMarketQuery, setMarketStart: workspace.setMarketStart, summary: workspace.summary, valuation: workspace.valuation, watchlist: workspace.watchlist}} />
        ) : activeView === 'journal' ? (
          <JournalPage workspace={{cancelEditingTrade: workspace.cancelEditingTrade, currency: workspace.currency, editingTrade: workspace.editingTrade, journalAnalytics: workspace.journalAnalytics, journalAssetClass: workspace.journalAssetClass, journalBreakdown: workspace.journalBreakdown, journalCurrency: workspace.journalCurrency, journalHasExit: workspace.journalHasExit, journalTrades: workspace.journalTrades, removeJournalTrade: workspace.removeJournalTrade, setJournalAssetClass: workspace.setJournalAssetClass, setJournalCurrency: workspace.setJournalCurrency, setJournalHasExit: workspace.setJournalHasExit, startEditingTrade: workspace.startEditingTrade, submitJournalTrade: workspace.submitJournalTrade, valuation: workspace.valuation}} />
        ) : portfolios.length === 0 ? (
          <PortfolioSetupPage workspace={{createPortfolio: workspace.createPortfolio}} />
        ) : activeView === 'overview' ? (
          <OverviewPage workspace={{addInstrumentToWatchlist: workspace.addInstrumentToWatchlist, cancelEditingTransaction: workspace.cancelEditingTransaction, coverage: workspace.coverage, csvContent: workspace.csvContent, csvPreview: workspace.csvPreview, currency: workspace.currency, editingTransaction: workspace.editingTransaction, exportTransactionCsv: workspace.exportTransactionCsv, importTransactionCsv: workspace.importTransactionCsv, importingCsv: workspace.importingCsv, loadTransactionCsv: workspace.loadTransactionCsv, loadingPortfolio: workspace.loadingPortfolio, marketDataStatus: workspace.marketDataStatus, portfolioId: workspace.portfolioId, previewTransactionCsv: workspace.previewTransactionCsv, refreshPortfolioValuation: workspace.refreshPortfolioValuation, refreshingValuation: workspace.refreshingValuation, removeInstrumentFromWatchlist: workspace.removeInstrumentFromWatchlist, riskMetrics: workspace.riskMetrics, setActiveView: workspace.setActiveView, setCoverageVersion: workspace.setCoverageVersion, setThesisSymbol: workspace.setThesisSymbol, setTransactionType: workspace.setTransactionType, submitTransaction: workspace.submitTransaction, summary: workspace.summary, thesisSymbol: workspace.thesisSymbol, transactionType: workspace.transactionType, valuation: workspace.valuation, valuationBySymbol: workspace.valuationBySymbol, watchlist: workspace.watchlist}} />
        ) : (
          <ActivityPage workspace={{cancelEditingTransaction: workspace.cancelEditingTransaction, csvContent: workspace.csvContent, csvPreview: workspace.csvPreview, currency: workspace.currency, editingTransaction: workspace.editingTransaction, exportTransactionCsv: workspace.exportTransactionCsv, importTransactionCsv: workspace.importTransactionCsv, importingCsv: workspace.importingCsv, loadTransactionCsv: workspace.loadTransactionCsv, loadingPortfolio: workspace.loadingPortfolio, marketDataStatus: workspace.marketDataStatus, portfolioId: workspace.portfolioId, previewTransactionCsv: workspace.previewTransactionCsv, refreshPortfolioValuation: workspace.refreshPortfolioValuation, refreshingValuation: workspace.refreshingValuation, riskMetrics: workspace.riskMetrics, removeTransaction: workspace.removeTransaction, setTransactionType: workspace.setTransactionType, startEditingTransaction: workspace.startEditingTransaction, submitTransaction: workspace.submitTransaction, summary: workspace.summary, transactionType: workspace.transactionType, transactions: workspace.transactions, valuation: workspace.valuation}} />
        )}
        <footer className="workspace-footer"><span><ArrowDownLeft size={14} /> No live orders</span><span>Data entered by you · {portfolios.length} portfolio{portfolios.length === 1 ? '' : 's'}</span></footer>
      </main>
      <nav className="mobile-nav" aria-label="Workspace">
        <button className={activeView === 'overview' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('overview')}><Wallet size={18} /><span>Overview</span></button>
        <button className={activeView === 'activity' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('activity')}><ReceiptText size={18} /><span>Transactions</span></button>
        <button className={activeView === 'journal' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('journal')}><Activity size={18} /><span>Journal</span></button>
        <button className={activeView === 'market-data' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('market-data')}><BarChart3 size={18} /><span>Markets</span></button>
        <button className={activeView === 'brokers' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('brokers')}><Landmark size={18} /><span>Brokers</span></button>
        <button className={activeView === 'alerts' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('alerts')}><BellRing size={18} /><span>Alerts</span></button>
        <button className={activeView === 'corporate-actions' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('corporate-actions')}><CalendarDays size={18} /><span>Actions</span></button>
        <button className={activeView === 'research' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('research')}><BookOpen size={18} /><span>Research</span></button>
      </nav>
    </div>
  );
}
