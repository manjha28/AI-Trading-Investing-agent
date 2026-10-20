import { Activity, BellRing, BookOpen, ChartCandlestick, ChevronRight, CircleDollarSign, Search } from 'lucide-react';

const modules = [
  { icon: Search, title: 'Investment research', description: 'Company fundamentals, valuation and risk analysis', status: 'Planned' },
  { icon: ChartCandlestick, title: 'Trading signals', description: 'Setups, backtests and paper trading', status: 'Planned' },
  { icon: BookOpen, title: 'Trading journal', description: 'Trades, net P&L and performance analytics', status: 'Planned' },
  { icon: BellRing, title: 'Market intelligence', description: 'Watchlists, earnings and price alerts', status: 'Planned' },
];

export default function App() {
  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand-mark"><Activity size={21} /></div>
        <div className="brand-copy"><strong>Trading Copilot</strong><span>Personal market intelligence</span></div>
        <span className="env-pill">LOCAL DEV</span>
      </header>

      <section className="hero">
        <p className="eyebrow">YOUR MARKETS, ONE WORKSPACE</p>
        <h1>Invest with a clearer picture.</h1>
        <p className="hero-copy">Research, trading analytics, journaling and market monitoring — designed to work together.</p>
        <div className="hero-note"><CircleDollarSign size={18} /><span>Read-only foundation · No live orders connected</span></div>
      </section>

      <section className="section-heading"><div><p className="eyebrow">WORKSPACE</p><h2>Your four modules</h2></div><span className="count">0 / 4 active</span></section>
      <section className="module-list">
        {modules.map(({ icon: Icon, title, description, status }) => (
          <article className="module-card" key={title}>
            <div className="module-icon"><Icon size={21} /></div>
            <div className="module-copy"><h3>{title}</h3><p>{description}</p><span className="status">{status}</span></div>
            <ChevronRight className="chevron" size={18} />
          </article>
        ))}
      </section>
      <footer><span className="pulse" /> API foundation next <span className="footer-dot">·</span> Market data not connected</footer>
    </main>
  );
}
