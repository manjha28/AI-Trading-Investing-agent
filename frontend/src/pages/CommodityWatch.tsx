import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { ApiRequestError, apiRequest } from '../api';
import { DayRange } from '../components/DayRange';
import { useCommodityStream, type CommodityStreamStatus, type CommodityTick } from '../hooks/useCommodityStream';

type Contract = {
  key: string; security_id: string; symbol: string; name: string | null; contract: string; expiry: string;
  days_to_expiry: number; lot_size: string | null; tick_size: string | null;
};
type Commodities = { count: number; exchange: string; hours_ist: string; session_open: boolean; session?: { open: boolean; reason: string | null; hours: string; next_open: string | null }; instruments: Contract[]; disclaimer: string };

const numeric = (value: string | null | undefined): number | null => {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};
const price = (value: string | null | undefined) => { const n = numeric(value); return n === null ? '—' : n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); };
const signed = (value: string | null | undefined, suffix = '') => { const n = numeric(value); return n === null ? '—' : `${n > 0 ? '+' : ''}${n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${suffix}`; };
const compact = (value: string | null | undefined) => { const n = numeric(value); return n === null ? '—' : new Intl.NumberFormat('en-IN', { notation: 'compact', maximumFractionDigits: 2 }).format(n); };
const tone = (value: string | null | undefined) => { const n = numeric(value); return n === null || n === 0 ? '' : n > 0 ? 'positive' : 'negative'; };
const stamp = (iso?: string | null) => iso ? new Date(iso).toLocaleString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) + ' IST' : null;
const clock = (ms: number) => new Date(ms).toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' });
/** MCX trades Mon-Fri 09:00-23:30 IST, until 23:55 once US daylight saving has ended (display hint; exchange holidays are caught by the tick-recency check below). */
function mcxCloseMinutes(at: Date): number {
  const zone = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', timeZoneName: 'short' }).formatToParts(at).find((p) => p.type === 'timeZoneName')?.value ?? '';
  return zone === 'EST' ? 23 * 60 + 55 : 23 * 60 + 30;
}
export function mcxSessionOpen(at: Date): boolean {
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(at);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? '';
  const minutes = Number(get('hour')) * 60 + Number(get('minute'));
  return !['Sat', 'Sun'].includes(get('weekday')) && minutes >= 9 * 60 && minutes < mcxCloseMinutes(at);
}
const STALE_AFTER_MS = 120_000;
const messageOf = (error: unknown) => (error instanceof ApiRequestError || error instanceof Error ? error.message : 'Request failed.');

type SortKey = 'symbol' | 'expiry' | 'ltp' | 'change' | 'change_pct' | 'volume';

type RowProps = { contract: Contract; tick: CommodityTick | undefined; selected: boolean; live: boolean; onSelect: (key: string) => void };

const CommodityRow = memo(function CommodityRow({ contract, tick, selected, live, onSelect }: RowProps) {
  const previous = useRef<string | null>(tick?.ltp ?? null);
  const [flash, setFlash] = useState<'up' | 'down' | ''>('');
  const ltp = tick?.ltp ?? null;
  useEffect(() => {
    if (ltp !== null && previous.current !== null && ltp !== previous.current) {
      setFlash(Number(ltp) > Number(previous.current) ? 'up' : 'down');
      const timer = window.setTimeout(() => setFlash(''), 700);
      previous.current = ltp;
      return () => window.clearTimeout(timer);
    }
    previous.current = ltp;
    return undefined;
  }, [ltp]);
  return (
    <tr className={`${selected ? 'watch-selected' : ''} ${tick ? '' : 'watch-nodata'}`} title={tick ? undefined : 'No trade yet — price unknown'} onClick={() => onSelect(contract.key)} aria-selected={selected}>
      <td className="watch-symbol"><span className={`watch-dot ${tick && live ? 'live' : ''}`} /><strong>{contract.symbol}</strong><small>{contract.name ?? contract.contract}</small></td>
      <td className="watch-num watch-age">{contract.expiry}<br /><small>{contract.days_to_expiry}d</small></td>
      <td className={`watch-num watch-ltp ${flash ? `flash-${flash}` : ''}`}>{price(tick?.ltp)}{tick?.last_close && <small title="Last close: the market is closed"> close</small>}</td>
      <td className="watch-num" title="Previous session close">{price(tick?.previous_close)}</td>
      <td className={`watch-num ${tone(tick?.change)}`}>{signed(tick?.change)}</td>
      <td className={`watch-num ${tone(tick?.change_pct)}`}>{signed(tick?.change_pct, '%')}</td>
      <td className="watch-range" aria-label="Day range"><DayRange low={tick?.low} high={tick?.high} last={tick?.ltp} /></td>
      <td className="watch-num">{compact(tick?.volume)}</td>
      <td className="watch-num" title="Open interest, last session">{compact(tick?.oi)}</td>
      <td className="watch-num watch-age" title={tick?.last_close ? 'Time of the last stored tick, or the last session when no tick was stored' : undefined}>{tick?.last_close ? (stamp(tick.tick_time) ?? tick.session_date ?? '—') : tick ? clock(tick.receivedAt) : '—'}</td>
    </tr>
  );
});

export function CommodityWatch() {
  const [data, setData] = useState<Commodities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sort, setSort] = useState<{ key: SortKey; dir: 'asc' | 'desc' }>({ key: 'symbol', dir: 'asc' });
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const { status: streamStatus, message: streamMessage, ticks, retry } = useCommodityStream(true);
  // The server decides the session once, at connect time, so a connection opened before the close would keep saying
  // LIVE. The label therefore follows the clock and the age of the newest tick, never a stale "active".
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 15_000); return () => window.clearInterval(timer); }, []);
  const open = data?.session ? data.session.open : mcxSessionOpen(new Date(now));   // the server's MCX calendar (holidays, evening-only days) wins over the clock guess
  const newestTick = Math.max(0, ...Object.values(ticks).map((t) => t.receivedAt));
  const quiet = open && streamStatus === 'active' && newestTick > 0 && now - newestTick > STALE_AFTER_MS;
  const status: CommodityStreamStatus = !open ? 'market_closed' : quiet ? 'stale' : streamStatus;
  const message = !open ? `Market closed · ${data?.session?.reason ?? 'outside MCX hours'}. Prices shown are the last close.`
    : streamStatus === 'no_data' ? (streamMessage ?? 'Feed not receiving data.')
    : quiet ? 'No ticks for over 2 minutes — the exchange may be on a break or holiday. Prices shown are the last received.' : streamMessage;

  useEffect(() => {
    apiRequest<Commodities>('/market-data/commodities').then(setData).catch((e: unknown) => setError(messageOf(e)));
  }, []);

  const contracts = data?.instruments ?? [];
  // Contracts with a price first (alphabetical, as delivered); contracts with no data yet sink to the bottom.
  const ordered = useMemo(() => {
    const value = (c: Contract): number | string | null => {
      const t = ticks[c.key];
      switch (sort.key) {
        case 'symbol': return c.symbol;
        case 'expiry': return c.expiry;
        case 'ltp': return numeric(t?.ltp);
        case 'change': return numeric(t?.change);
        case 'change_pct': return numeric(t?.change_pct);
        case 'volume': return numeric(t?.volume);
      }
    };
    const priced = contracts.filter((c) => ticks[c.key] !== undefined);
    const unpriced = contracts.filter((c) => ticks[c.key] === undefined);
    const dir = sort.dir === 'asc' ? 1 : -1;
    // unknown values are always last, whatever the direction
    const compare = (a: Contract, b: Contract) => {
      const x = value(a), y = value(b);
      if (x === null && y === null) return 0;
      if (x === null) return 1;
      if (y === null) return -1;
      return (typeof x === 'string' ? x.localeCompare(String(y)) : x - Number(y)) * dir;
    };
    return [[...priced].sort(compare), unpriced].flat();
  }, [contracts, ticks, sort]);
  const sortBy = (key: SortKey) => setSort((cur) => ({ key, dir: cur.key === key && cur.dir === 'desc' ? 'asc' : 'desc' }));
  const arrow = (key: SortKey) => (sort.key === key ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : '');
  const stats = useMemo(() => {
    const values = Object.values(ticks);
    return { live: values.length, up: values.filter((t) => (numeric(t.change_pct) ?? 0) > 0).length, down: values.filter((t) => (numeric(t.change_pct) ?? 0) < 0).length };
  }, [ticks]);
  const selected = contracts.find((c) => c.key === selectedKey) ?? null;
  const tick = selected ? ticks[selected.key] : undefined;
  const label = status === 'active' ? 'LIVE' : status === 'stale' ? 'NO RECENT TICKS' : status === 'market_closed' ? 'MARKET CLOSED' : status === 'no_data' ? 'FEED NOT RECEIVING DATA' : status === 'unavailable' ? 'FEED UNAVAILABLE' : status === 'idle' ? 'IDLE' : 'CONNECTING';

  return (
    <div className="watch-page">
      <div className="watch-strip" role="status">
        <span className={`watch-status watch-status-${status}`}>● {label}</span>
        <span><b>{contracts.length}</b> contracts</span>
        <span><b>{stats.live}</b> {status === 'active' ? 'live' : 'with prices'}</span>
        <span className="positive">▲ <b>{stats.up}</b></span>
        <span className="negative">▼ <b>{stats.down}</b></span>
        <span className="watch-note">MCX · {data?.hours_ist ?? '09:00–23:30 IST'} · front-month futures · Dhan live feed · decision support, not investment advice</span>
      </div>
      {status === 'market_closed' && <p className="watch-note" role="status">Market closed · last close {Object.values(ticks).map((t) => stamp(t.tick_time) ?? t.session_date).find(Boolean) ?? 'time unknown'}{Object.values(ticks).some((t) => t.session_date && !t.tick_time) ? ' (session date from Dhan daily data)' : ''}</p>}
      {(error || (message && status !== 'active')) && <p className="alerts-notice" role="alert">{error ?? message}{open && (status === 'no_data' || status === 'unavailable') && <> <button type="button" className="secondary-button" onClick={retry}>Retry</button></>}</p>}
      <div className="watch-split">
        <section className="data-section watch-table-panel" aria-label="Commodity watch">
          <div className="watch-scroll">
            <table className="watch-table commodity-table">
              <thead><tr>
                <th><button type="button" onClick={() => sortBy('symbol')}>Commodity{arrow('symbol')}</button></th>
                <th className="watch-num"><button type="button" onClick={() => sortBy('expiry')}>Expiry{arrow('expiry')}</button></th>
                <th className="watch-num"><button type="button" onClick={() => sortBy('ltp')}>LTP{arrow('ltp')}</button></th>
                <th className="watch-num">Prev close</th>
                <th className="watch-num"><button type="button" onClick={() => sortBy('change')}>Chg{arrow('change')}</button></th>
                <th className="watch-num"><button type="button" onClick={() => sortBy('change_pct')}>Chg %{arrow('change_pct')}</button></th>
                <th>Day range</th>
                <th className="watch-num"><button type="button" onClick={() => sortBy('volume')}>Volume{arrow('volume')}</button></th>
                <th className="watch-num">OI</th>
                <th className="watch-num">Updated (IST)</th>
              </tr></thead>
              <tbody>{ordered.map((c) => <CommodityRow key={c.key} contract={c} tick={ticks[c.key]} selected={c.key === selectedKey} live={status === 'active'} onSelect={setSelectedKey} />)}</tbody>
            </table>
            {data && contracts.length === 0 && <p className="table-empty">No MCX contracts are stored. Refresh the derivatives master first.</p>}
          </div>
        </section>
        <aside className="data-section watch-chart-panel" aria-label="Commodity detail">
          {!selected ? <p className="table-empty">Select a commodity to see its day statistics.</p> : (
            <>
              <div className="section-heading"><div><p className="eyebrow">MCX · front-month future</p><h3>{selected.symbol}</h3></div></div>
              <p className="watch-quote"><span className="watch-big">{price(tick?.ltp)}</span> <span className={tone(tick?.change)}>{signed(tick?.change)} ({signed(tick?.change_pct, '%')})</span></p>
              <dl className="watch-stats">
                <div><dt>Previous close</dt><dd>{price(tick?.previous_close)}</dd></div>
                <div><dt>Open</dt><dd>{price(tick?.open)}</dd></div>
                <div><dt>High</dt><dd>{price(tick?.high)}</dd></div>
                <div><dt>Low</dt><dd>{price(tick?.low)}</dd></div>
                <div><dt>Volume</dt><dd>{compact(tick?.volume)}</dd></div>
                <div><dt>Open interest</dt><dd>{compact(tick?.oi)}</dd></div>
                <div><dt>Lot size</dt><dd>{selected.lot_size ?? '—'}</dd></div>
                <div><dt>Tick size (paise)</dt><dd>{selected.tick_size ?? '—'}</dd></div>
                <div><dt>Expiry</dt><dd>{selected.expiry} ({selected.days_to_expiry}d)</dd></div>
              </dl>
              <p className="crypto-sub">Change is measured against Dhan&apos;s reference close for the contract. MCX history is not stored yet, so there is no chart; prices update live while MCX is open.</p>
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
