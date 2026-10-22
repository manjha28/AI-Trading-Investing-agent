import { Plus, Wallet } from 'lucide-react';
import type { WorkspaceState } from '../components/useWorkspace';

export function PortfolioSetupPage({ workspace }: { workspace: Pick<WorkspaceState, 'createPortfolio'> }) {
  const { createPortfolio } = workspace;
  return (
              <section className="empty-workspace">
                <div className="section-icon"><Wallet size={22} /></div>
                <p className="eyebrow">FIRST PORTFOLIO</p>
                <h2>Start with a clean ledger.</h2>
                <p>Balances and positions will be derived only from the transactions you enter.</p>
                <form className="portfolio-create" onSubmit={createPortfolio}>
                  <label>Portfolio name<input name="name" placeholder="Long-term account" maxLength={100} required /></label>
                  <label>Base currency<select name="base_currency"><option value="USD">USD · US dollar</option><option value="INR">INR · Indian rupee</option></select></label>
                  <button className="primary-button" type="submit"><Plus size={17} /> Create portfolio</button>
                </form>
              </section>
  );
}
