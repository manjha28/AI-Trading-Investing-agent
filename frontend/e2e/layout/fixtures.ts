// TEST DATA ONLY. Hand-written cards that vary on purpose (missing T3, long names, long status text, no chips, an unavailable ladder) to prove card layout; never a runtime path.
const lv = (over: Record<string, unknown> = {}) => ({ status: 'available', entry_zone: { low: '100.00', high: '103.00' }, stop: '95.00', reference_close: '101.50', prior_high_reference: '110.00',
  targets: [{ label: 'T1', price: '115.00', risk_reward: 2 }, { label: 'T2', price: '125.00', risk_reward: 3.5 }, { label: 'T3', price: '140.00', risk_reward: 5 }], ...over });
const conf = (score: number, extra: Record<string, unknown> = {}) => ({ score, band: 'High', note: 'Stop is 2.0 ATR away.', win_confidence: { line: 'Measured win rate 31% (avg setup 29%) · based on 118 similar past setups', band: 'Medium', band_label: 'Unproven · 118 similar setups' }, parts: [{ key: 'a', label: 'Evidence quality', value: 80, weight: 0.4 }, { key: 'b', label: 'Signal strength', value: 55, weight: 0.25 }], ...extra });
const hz = (c: unknown, rec = 'BUY') => ({ recommendation: rec, score: 80, confidence: 80, buy_confidence: rec === 'BUY' ? c : null, why_not: rec === 'BUY' ? [] : ['Price is extended above the 20-day average, so a new entry here has poor risk to reward.'] });
const base = (n: number, over: Record<string, unknown> = {}) => ({ instrument_id: `i${n}`, symbol: `SYM${n}`, name: `Company number ${n} Limited`, held: false, quantity: null, bucket: 'buy', action: 'Can be bought', researched_at: '2026-11-24T11:00:00Z', stale: false,
  last_close: '101.50', price: { ltp: '101.50', change: '1.20', change_pct: '1.20' }, horizons: { swing: hz(conf(70), 'HOLD'), positional: hz(conf(72.4)), long_term: hz(conf(60), 'AVOID') }, levels: { positional: lv() }, confidence: conf(72.4), ...over });
export const buyRows = [
  base(1),
  base(2, { name: 'A very long company name that would normally push the card wider than its column Industries Private Limited', held: true, quantity: '25' }),
  base(3, { levels: { positional: lv({ targets: [{ label: 'T1', price: '115.00', risk_reward: 2 }, { label: 'T2', price: '125.00', risk_reward: 3.5 }] }) } }),                  // missing T3
  base(4, { levels: { positional: lv({ event_gate: 'results on 2026-11-26 (2 sessions ahead): no entry-now signal until it has passed. Results can move the price sharply in either direction.', pivot_support: { name: 'S1', price: '98.00' }, pivot_resistance: { name: 'R1', price: '112.00' } }) } }),
  base(5, { stale: true, held: true, quantity: '10' }),
  base(6, { levels: { positional: { status: 'unavailable', reason: 'No levels for this horizon: the stop would be wider than the horizon allows.' } } }),
  base(7, { horizons: { swing: hz(conf(70)), positional: hz(conf(66)), long_term: hz(conf(60)) }, levels: { swing: lv(), positional: lv(), long_term: lv() } }),
  base(8, { price: null, last_close: null }),
];
export const holdRows = [1, 2, 3].map((n) => base(100 + n, { bucket: 'hold', held: true, quantity: '10', action: 'In your portfolio: keep holding.', horizons: { swing: hz(conf(50), 'HOLD'), positional: hz(conf(50), 'HOLD'), long_term: hz(conf(50), 'HOLD') }, levels: {}, confidence: null }));
export const radarRows = [1, 2].map((n) => base(200 + n, { bucket: 'radar', action: 'Put on radar: a buy signal, no entry range yet.', radar_reasons: ['Waiting for a pullback to the 20-day average'], levels: {} }));
export const report = {
  as_of: '2026-11-24T12:00:00Z', scope: 'test', disclaimer: 'Decision support, not investment advice.',
  buckets: { buy: buyRows, setup_waiting: [], radar: radarRows, hold: holdRows, no_new_entry: [], stale_data: [], not_enough_data: [] },
  counts: { buy: buyRows.length, setup_waiting: 0, radar: radarRows.length, hold: holdRows.length, no_new_entry: 0, stale_data: 0, not_enough_data: 0 },
};
