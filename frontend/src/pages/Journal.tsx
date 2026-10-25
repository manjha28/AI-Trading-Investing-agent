import { useMemo, useState } from 'react';
import {
  CalendarDays, ChevronLeft, ChevronRight, CircleDollarSign, Pencil, Plus, Search, Trash2, X,
} from 'lucide-react';
import {
  formatMoney, formatQuantity, localDateTime, type WorkspaceState,
} from '../components/useWorkspace';

type JournalTrade = WorkspaceState['journalTrades'][number];
type SortKey = 'instrument' | 'strategy' | 'setup' | 'side' | 'asset_class' | 'quantity' | 'entry_time' | 'exit_time' | 'net_pnl';

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const numberValue = (value: string | null | undefined) => value === null || value === undefined ? null : Number(value);

function localDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function activityDate(trade: JournalTrade): string {
  return localDate(trade.exit_time ?? trade.entry_time);
}

function optionDescription(trade: JournalTrade): string {
  if (trade.asset_class !== 'option') return trade.asset_class;
  return [trade.option_type, trade.strike, trade.expiry?.slice(0, 10)].filter(Boolean).join(' · ');
}

export function JournalPage({ workspace }: { workspace: Pick<WorkspaceState, 'cancelEditingTrade' | 'currency' | 'editingTrade' | 'journalAnalytics' | 'journalAssetClass' | 'journalBreakdown' | 'journalCurrency' | 'journalHasExit' | 'journalTrades' | 'removeJournalTrade' | 'setJournalAssetClass' | 'setJournalCurrency' | 'setJournalHasExit' | 'startEditingTrade' | 'submitJournalTrade' | 'valuation'> }) {
  const {
    cancelEditingTrade,
    editingTrade,
    journalAnalytics,
    journalAssetClass,
    journalBreakdown,
    journalCurrency,
    journalHasExit,
    journalTrades,
    removeJournalTrade,
    setJournalAssetClass,
    setJournalCurrency,
    setJournalHasExit,
    startEditingTrade,
    submitJournalTrade,
  } = workspace;
  const [entryOpen, setEntryOpen] = useState(false);
  const [strategyFilter, setStrategyFilter] = useState('');
  const [setupFilter, setSetupFilter] = useState('');
  const [sideFilter, setSideFilter] = useState('');
  const [assetFilter, setAssetFilter] = useState('');
  const [search, setSearch] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [selectedDate, setSelectedDate] = useState('');
  const [calendarMonth, setCalendarMonth] = useState(() => {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), 1);
  });
  const [sort, setSort] = useState<{ key: SortKey; direction: 'asc' | 'desc' }>({
    key: 'entry_time',
    direction: 'desc',
  });

  const strategies = useMemo(
    () => [...new Set(journalTrades.map((trade) => trade.strategy?.trim()).filter((value): value is string => Boolean(value)))].sort((a, b) => a.localeCompare(b)),
    [journalTrades],
  );
  const setups = useMemo(
    () => [...new Set(journalTrades.map((trade) => trade.setup?.trim()).filter((value): value is string => Boolean(value)))].sort((a, b) => a.localeCompare(b)),
    [journalTrades],
  );
  const dayCounts = useMemo(() => {
    const counts = new Map<string, number>();
    const query = search.trim().toLocaleLowerCase();
    for (const trade of journalTrades) {
      if (trade.currency !== journalCurrency
        || (strategyFilter && trade.strategy !== strategyFilter)
        || (setupFilter && trade.setup !== setupFilter)
        || (sideFilter && trade.side !== sideFilter)
        || (assetFilter && trade.asset_class !== assetFilter)) continue;
      const day = activityDate(trade);
      if (!day || (dateFrom && day < dateFrom) || (dateTo && day > dateTo)) continue;
      if (query && ![trade.instrument, trade.strategy, trade.setup, trade.notes, ...trade.tags].some((value) => value?.toLocaleLowerCase().includes(query))) continue;
      counts.set(day, (counts.get(day) ?? 0) + 1);
    }
    return counts;
  }, [journalTrades, journalCurrency, strategyFilter, setupFilter, sideFilter, assetFilter, dateFrom, dateTo, search]);
  const filteredTrades = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    const rows = journalTrades.filter((trade) => {
      const day = activityDate(trade);
      return trade.currency === journalCurrency
        && (!strategyFilter || trade.strategy === strategyFilter)
        && (!setupFilter || trade.setup === setupFilter)
        && (!sideFilter || trade.side === sideFilter)
        && (!assetFilter || trade.asset_class === assetFilter)
        && (!dateFrom || day >= dateFrom)
        && (!dateTo || day <= dateTo)
        && (!selectedDate || day === selectedDate)
        && (!query || [trade.instrument, trade.strategy, trade.setup, trade.notes, ...trade.tags].some((value) => value?.toLocaleLowerCase().includes(query)));
    });
    const direction = sort.direction === 'asc' ? 1 : -1;
    return rows.sort((left, right) => {
      const first = left[sort.key];
      const second = right[sort.key];
      if (sort.key === 'net_pnl' || sort.key === 'quantity') {
        const a = numberValue(first as string | null);
        const b = numberValue(second as string | null);
        if (a === null || b === null) return a === b ? 0 : a === null ? 1 : -1;
        return (a - b) * direction;
      }
      return String(first ?? '').localeCompare(String(second ?? ''), undefined, { numeric: true }) * direction;
    });
  }, [journalTrades, journalCurrency, strategyFilter, setupFilter, sideFilter, assetFilter, dateFrom, dateTo, selectedDate, search, sort]);

  const year = calendarMonth.getFullYear();
  const month = calendarMonth.getMonth();
  const monthDayCount = new Date(year, month + 1, 0).getDate();
  const leadingDays = new Date(year, month, 1).getDay();
  const calendarDays: (number | null)[] = [
    ...Array.from({ length: leadingDays }, () => null),
    ...Array.from({ length: monthDayCount }, (_, index) => index + 1),
  ];
  while (calendarDays.length % 7 !== 0) calendarDays.push(null);
  const monthLabel = calendarMonth.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });

  function toggleSort(key: SortKey) {
    setSort((current) => ({
      key,
      direction: current.key === key && current.direction === 'asc' ? 'desc' : 'asc',
    }));
  }

  function clearFilters() {
    setStrategyFilter('');
    setSetupFilter('');
    setSideFilter('');
    setAssetFilter('');
    setSearch('');
    setDateFrom('');
    setDateTo('');
    setSelectedDate('');
  }

  const sortButton = (label: string, key: SortKey) => (
    <button className="journal-sort-button" type="button" onClick={() => toggleSort(key)}>
      {label}{sort.key === key ? <span aria-hidden="true">{sort.direction === 'asc' ? ' ↑' : ' ↓'}</span> : null}
    </button>
  );

  return (
    <div className="journal-page">
      <header className="journal-heading">
        <div>
          <p className="eyebrow">TRADE REVIEW</p>
          <h2>Trading journal</h2>
          <p>Review recorded decisions, realized outcomes, and patterns across your trades.</p>
        </div>
        <button className="primary-button journal-add-button" type="button" onClick={() => {
          if (editingTrade) cancelEditingTrade();
          setEntryOpen(true);
          setJournalHasExit(false);
        }}>
          <Plus size={16} /> Add trade
        </button>
      </header>

      <div className="journal-toolbar" aria-label="Trade filters">
        <label>From<input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} /></label>
        <label>To<input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} /></label>
        <label>Currency<select value={journalCurrency} onChange={(event) => setJournalCurrency(event.target.value)}><option value="USD">USD</option><option value="INR">INR</option></select></label>
        <label>Strategy<select value={strategyFilter} onChange={(event) => setStrategyFilter(event.target.value)}><option value="">All strategies</option>{strategies.map((strategy) => <option value={strategy} key={strategy}>{strategy}</option>)}</select></label>
        <label>Setup<select value={setupFilter} onChange={(event) => setSetupFilter(event.target.value)}><option value="">All setups</option>{setups.map((setup) => <option value={setup} key={setup}>{setup}</option>)}</select></label>
        <label>Side<select value={sideFilter} onChange={(event) => setSideFilter(event.target.value)}><option value="">All sides</option><option value="long">Long</option><option value="short">Short</option></select></label>
        <label>Market<select value={assetFilter} onChange={(event) => setAssetFilter(event.target.value)}><option value="">All markets</option><option value="equity">Equity</option><option value="future">Future</option><option value="option">Option</option><option value="crypto">Crypto</option></select></label>
        {(dateFrom || dateTo || selectedDate || strategyFilter || setupFilter || sideFilter || assetFilter || search) && <button className="journal-clear-filters" type="button" onClick={clearFilters}><X size={13} /> Clear filters</button>}
      </div>

      <div className="data-note journal-data-note"><CircleDollarSign size={16} /><span>Figures use saved trades and existing analytics only. No market prices are fetched; account attribution and daily P&amp;L aggregates are not available in the current journal API.</span></div>

      {journalAnalytics && (
        <section className="journal-kpis" aria-label="Journal performance">
          <article className="journal-kpi journal-kpi-primary"><span>Net realized P&amp;L</span><strong className={Number(journalAnalytics.net_pnl) >= 0 ? 'positive' : 'negative'}>{formatMoney(journalAnalytics.net_pnl, journalCurrency)}</strong><small>{journalCurrency} · all closed trades</small></article>
          <article className="journal-kpi"><span>Win rate</span><strong>{journalAnalytics.win_rate === null ? '—' : `${journalAnalytics.win_rate}%`}</strong><small>{journalAnalytics.wins} wins · {journalAnalytics.losses} losses</small></article>
          <article className="journal-kpi"><span>Profit factor</span><strong>{journalAnalytics.profit_factor ?? '—'}</strong><small>Closed-trade analytics</small></article>
          <article className="journal-kpi"><span>Expectancy</span><strong>{journalAnalytics.expectancy === null ? '—' : formatMoney(journalAnalytics.expectancy, journalCurrency)}</strong><small>Per closed trade</small></article>
          <article className="journal-kpi"><span>Max drawdown</span><strong className={Number(journalAnalytics.max_drawdown) > 0 ? 'negative' : ''}>{formatMoney(journalAnalytics.max_drawdown, journalCurrency)}</strong><small>Closed-trade sequence</small></article>
          <article className="journal-kpi"><span>Trades logged</span><strong>{journalAnalytics.closed_trades + journalAnalytics.open_trades}</strong><small>{journalAnalytics.closed_trades} closed · {journalAnalytics.open_trades} open</small></article>
        </section>
      )}

      <div className="journal-main-grid">
        <section className="data-section journal-calendar-panel">
          <div className="section-heading">
            <div><p className="eyebrow">TRADE ACTIVITY</p><h2>Monthly calendar</h2></div>
            <div className="journal-calendar-controls">
              <button type="button" aria-label="Previous month" onClick={() => setCalendarMonth(new Date(year, month - 1, 1))}><ChevronLeft size={16} /></button>
              <strong>{monthLabel}</strong>
              <button type="button" aria-label="Next month" onClick={() => setCalendarMonth(new Date(year, month + 1, 1))}><ChevronRight size={16} /></button>
            </div>
          </div>
          <div className="journal-calendar-grid" role="grid" aria-label={`${monthLabel} trade activity calendar`}>
            {WEEKDAYS.map((day) => <span className="journal-weekday" role="columnheader" key={day}>{day}</span>)}
            {calendarDays.map((day, index) => {
              if (day === null) return <span className="journal-calendar-empty" role="gridcell" key={`empty-${index}`} />;
              const date = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
              const count = dayCounts.get(date) ?? 0;
              return (
                <button
                  className={`journal-calendar-day${count ? ' has-trades' : ''}${selectedDate === date ? ' selected' : ''}`}
                  type="button"
                  role="gridcell"
                  aria-label={`${date}, ${count} recorded trades${selectedDate === date ? ', selected' : ''}`}
                  aria-pressed={selectedDate === date}
                  key={date}
                  onClick={() => setSelectedDate((current) => current === date ? '' : date)}
                >
                  <span>{day}</span>
                  {count > 0 && <small>{count} trade{count === 1 ? '' : 's'}</small>}
                </button>
              );
            })}
          </div>
          <p className="journal-calendar-note"><CalendarDays size={13} /> Select a date to filter the history. Closed trades use exit date; open trades use entry date. Daily P&amp;L aggregates are not provided by the current API.</p>
        </section>

        <section className="data-section journal-breakdown-panel">
          <div className="section-heading"><div><p className="eyebrow">PERFORMANCE REVIEW</p><h2>By strategy</h2></div></div>
          {journalBreakdown.length === 0 ? <div className="table-empty">No strategy results for {journalCurrency}.</div> : (
            <div className="table-scroll">
              <table>
                <thead><tr><th>Strategy</th><th>Closed</th><th>W / L</th><th>Net P&amp;L</th><th>Profit factor</th></tr></thead>
                <tbody>{journalBreakdown.map((row) => <tr key={row.key}>
                  <td className="instrument-cell">{row.key}</td><td>{row.closed_trades}</td><td>{row.wins} / {row.losses}</td>
                  <td className={Number(row.net_pnl) >= 0 ? 'positive' : 'negative'}>{formatMoney(row.net_pnl, journalCurrency)}</td>
                  <td>{row.profit_factor ?? '—'}</td>
                </tr>)}</tbody>
              </table>
            </div>
          )}
          <div className="journal-limitation">The current API groups analytics by strategy only; setup/side breakdown, average win/loss and equity-curve points are not provided. Screenshots and account attribution are not part of the current trade model.</div>
        </section>
      </div>

      <section className="data-section journal-history-panel">
        <div className="section-heading journal-history-heading">
          <div><p className="eyebrow">TRADE HISTORY</p><h2>Recorded trades <span className="section-count">{filteredTrades.length} shown</span></h2></div>
          <label className="journal-search"><Search size={15} /><input aria-label="Search trades" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search instrument, setup, notes…" /></label>
        </div>
        {filteredTrades.length === 0 ? <div className="table-empty">{journalTrades.length ? 'No trades match these filters.' : 'No journal trades recorded yet.'}</div> : (
          <div className="table-scroll journal-table-scroll">
            <table className="journal-table">
              <thead><tr>
                <th>{sortButton('Instrument', 'instrument')}</th><th>{sortButton('Market', 'asset_class')}</th><th>{sortButton('Side', 'side')}</th>
                <th>{sortButton('Size', 'quantity')}</th><th>{sortButton('Entry', 'entry_time')}</th><th>{sortButton('Exit', 'exit_time')}</th>
                <th>{sortButton('Net P&L', 'net_pnl')}</th><th>Fees</th><th>{sortButton('Strategy / setup', 'strategy')}</th><th>Notes &amp; tags</th><th>Actions</th>
              </tr></thead>
              <tbody>{filteredTrades.map((trade) => (
                <tr key={trade.id}>
                  <td className="instrument-cell">{trade.instrument}<small className="trade-subtext">{trade.currency}</small></td>
                  <td>{optionDescription(trade)}</td>
                  <td><span className={`journal-side-badge ${trade.side === 'long' ? 'long' : 'short'}`}>{trade.side}</span></td>
                  <td>{formatQuantity(trade.quantity)} × {formatQuantity(trade.multiplier)}</td>
                  <td>{new Date(trade.entry_time).toLocaleString()}</td>
                  <td>{trade.exit_time ? new Date(trade.exit_time).toLocaleString() : <span className="journal-open-badge">Open</span>}</td>
                  <td className={trade.net_pnl === null ? '' : Number(trade.net_pnl) >= 0 ? 'positive' : 'negative'}>{trade.net_pnl === null ? '—' : formatMoney(trade.net_pnl, trade.currency)}</td>
                  <td>{formatMoney(trade.fees, trade.currency)}</td>
                  <td>{trade.strategy || '—'}<small className="trade-subtext">{trade.setup || 'No setup'}</small></td>
                  <td className="journal-notes-cell">
                    <div className="journal-tags">{trade.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
                    {trade.notes && <details><summary>View notes</summary><p>{trade.notes}</p></details>}
                  </td>
                  <td className="journal-row-actions">
                    <button className="icon-button" type="button" onClick={() => {
                      startEditingTrade(trade);
                      setEntryOpen(true);
                    }} aria-label={`Edit ${trade.instrument} trade`}><Pencil size={14} /></button>
                    <button className="icon-button" type="button" onClick={() => void removeJournalTrade(trade.id)} aria-label={`Delete ${trade.instrument} trade`}><Trash2 size={14} /></button>
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>

      {(entryOpen || editingTrade) && (
        <section className="entry-section journal-entry-panel" aria-label={editingTrade ? 'Edit trade' : 'Add trade'}>
          <div className="section-heading">
            <div><p className="eyebrow">{editingTrade ? 'EDIT TRADE' : 'QUICK ENTRY'}</p><h2>{editingTrade ? `Edit ${editingTrade.instrument}` : 'Log a trade'}</h2></div>
            <button className="icon-button" type="button" aria-label="Close trade form" onClick={() => {
              cancelEditingTrade();
              setEntryOpen(false);
              setJournalHasExit(false);
            }}><X size={16} /></button>
          </div>
          <form key={editingTrade?.id ?? 'new-trade'} className="form-stack journal-form" onSubmit={submitJournalTrade}>
            <label>Market<select name="asset_class" value={journalAssetClass} onChange={(event) => setJournalAssetClass(event.target.value)}><option value="equity">Equity</option><option value="future">Future</option><option value="option">Option</option><option value="crypto">Crypto</option></select></label>
            <label>Currency<select name="currency" defaultValue={editingTrade?.currency ?? 'USD'}><option value="USD">USD</option><option value="INR">INR</option></select></label>
            <label>Instrument<input name="instrument" placeholder="Symbol or contract" maxLength={64} defaultValue={editingTrade?.instrument ?? ''} required /></label>
            <label>Side<select name="side" defaultValue={editingTrade?.side ?? 'long'}><option value="long">Long</option><option value="short">Short</option></select></label>
            <label>Quantity<input name="quantity" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.quantity ?? ''} required /></label>
            <label>Multiplier<input name="multiplier" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.multiplier ?? '1'} required /></label>
            <label>Entry price<input name="entry_price" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.entry_price ?? ''} required /></label>
            <label>Entry time<input name="entry_time" type="datetime-local" defaultValue={localDateTime(editingTrade?.entry_time) || new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16)} required /></label>
            {journalHasExit && <>
              <label>Exit price<input name="exit_price" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.exit_price ?? ''} required /></label>
              <label>Exit time<input name="exit_time" type="datetime-local" defaultValue={localDateTime(editingTrade?.exit_time)} required /></label>
            </>}
            {journalAssetClass === 'option' && <>
              <label>Option type<select name="option_type" defaultValue={editingTrade?.option_type ?? 'call'}><option value="call">Call</option><option value="put">Put</option></select></label>
              <label>Strike<input name="strike" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.strike ?? ''} required /></label>
              <label>Expiry<input name="expiry" type="date" defaultValue={editingTrade?.expiry?.slice(0, 10) ?? ''} required /></label>
            </>}
            <label>Fees paid<input name="fees" type="number" min="0" step="0.01" defaultValue={editingTrade?.fees ?? '0.00'} required /></label>
            <label>Planned risk<input name="planned_risk" type="number" min="0.01" step="0.01" defaultValue={editingTrade?.planned_risk ?? ''} /></label>
            <label>Strategy<input name="strategy" maxLength={100} defaultValue={editingTrade?.strategy ?? ''} /></label>
            <label>Setup<input name="setup" maxLength={100} defaultValue={editingTrade?.setup ?? ''} /></label>
            <label>Tags<input name="tags" placeholder="patient, breakout" defaultValue={editingTrade?.tags.join(', ') ?? ''} /></label>
            <label className="journal-form-notes">Notes<textarea name="notes" maxLength={4000} defaultValue={editingTrade?.notes ?? ''} rows={3} /></label>
            <label className="checkbox-line journal-form-closed"><input type="checkbox" checked={journalHasExit} onChange={(event) => setJournalHasExit(event.target.checked)} /> Trade is closed</label>
            <button className="primary-button journal-save-button" type="submit"><Plus size={15} /> {editingTrade ? 'Update trade' : 'Save trade'}</button>
          </form>
        </section>
      )}
    </div>
  );
}
