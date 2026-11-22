import { Activity, BookOpen, Landmark, LogOut, MoreHorizontal, Target, TrendingUp, Wallet } from 'lucide-react';
import { useState } from 'react';

export type NavView = 'overview' | 'journal' | 'market-data' | 'brokers' | 'alerts' | 'corporate-actions' | 'research' | 'signals' | 'crypto-delta' | 'market-watch';
type Props = { activeView: NavView; setActiveView: (view: NavView) => void; onSignOut: () => void };

/** Bottom navigation for phones: one row of five icons (Overview, Watch, Research, Signals, More). Journal, Crypto, the Dhan connection page and Sign out live in the More sheet. */
export function MobileNav({ activeView, setActiveView, onSignOut }: Props) {
  const [more, setMore] = useState(false);
  const go = (view: NavView) => { setMore(false); setActiveView(view); };
  const inMore = ['journal', 'crypto-delta', 'brokers'].includes(activeView);
  const item = (view: NavView, label: string, icon: React.ReactNode, active: boolean) => (
    <button key={view} type="button" className={active ? 'nav-item active' : 'nav-item'} aria-current={active ? 'page' : undefined} onClick={() => go(view)}>{icon}<span>{label}</span></button>
  );
  return (
    <>
      {more && (
        <div className="mobile-more-sheet" role="dialog" aria-label="More">
          <button type="button" className="mobile-more-item" onClick={() => go('journal')}><Activity size={17} /> Trade journal</button>
          <button type="button" className="mobile-more-item" onClick={() => go('crypto-delta')}>₿ Crypto · Delta</button>
          <button type="button" className="mobile-more-item" onClick={() => go('brokers')}><Landmark size={17} /> Dhan connection</button>
          <button type="button" className="mobile-more-item" onClick={() => { setMore(false); onSignOut(); }}><LogOut size={17} /> Sign out</button>
        </div>
      )}
      <nav className="mobile-nav" aria-label="Workspace">
        {item('overview', 'Overview', <Wallet size={18} />, activeView === 'overview')}
        {item('market-watch', 'Watch', <TrendingUp size={18} />, activeView === 'market-watch' || activeView === 'market-data')}
        {item('research', 'Research', <BookOpen size={18} />, activeView === 'research')}
        {item('signals', 'Signals', <Target size={18} />, activeView === 'signals')}
        <button type="button" className={more || inMore ? 'nav-item active' : 'nav-item'} aria-expanded={more} onClick={() => setMore((open) => !open)}><MoreHorizontal size={18} /><span>More</span></button>
      </nav>
    </>
  );
}
