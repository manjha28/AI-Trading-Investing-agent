/** The effective execution state per venue, as the backend reports it (services/execution_state.py). Every screen prints THIS, never its own flag logic or a fixed sentence. */
export type EffectiveState = 'OFF' | 'ARMED_BUT_BLOCKED' | 'LIVE';
export type VenueState = {
  venue: 'dhan' | 'delta'; label: string; deployment_flag: { name: string; enabled: boolean }; armed: boolean; armed_at: string | null;
  window: { kind: string; text: string; open_now: boolean }; strategies: { key: string; live_tests: number }[]; live_tests: number;
  effective: { state: EffectiveState; reasons: string[] }; limits: Record<string, string | number>; today: { sent_to_broker: number; rejected_by_broker: number; blocked_attempts: number };
};
export type ExecutionStates = { dhan: VenueState; delta: VenueState; as_of: string };

export const SHORT: Record<EffectiveState, string> = { OFF: 'OFF', ARMED_BUT_BLOCKED: 'ARMED BUT BLOCKED', LIVE: 'LIVE' };
export const venueName = (v: VenueState) => (v.venue === 'dhan' ? 'Dhan' : 'Delta');

/** The hover text on a venue's button: what the state means and how to change it. */
export function explainState(v: VenueState): string {
  const n = venueName(v);
  const meaning = {
    OFF: `OFF: live orders are not armed, so nothing is sent to ${n}.`,
    ARMED_BUT_BLOCKED: `ARMED BUT BLOCKED: you turned live orders on, but something else still stops them, so nothing is sent to ${n}.`,
    LIVE: `LIVE: real-money orders can be sent to ${n} now.`,
  }[v.effective.state];
  const how = v.armed
    ? `Click to turn off: it disarms at once and cancels this app's unfilled ${n} orders. Filled positions stay open.`
    : 'Click to turn on: a warning appears and you confirm it. A strategy must also be switched on for live before any order goes out.';
  return [meaning, ...v.effective.reasons.map((r) => `• ${r}`), how].join('\n');
}

/** "Dhan OFF · Delta LIVE" for the sidebar. */
export function sidebarWording(s: ExecutionStates | null): string {
  if (!s) return 'Execution: checking…';
  return `Execution · ${venueName(s.dhan)} ${SHORT[s.dhan.effective.state]} · ${venueName(s.delta)} ${SHORT[s.delta.effective.state]}`;
}

/** The footer line with the reason for every venue that is not LIVE. */
export function footerWording(s: ExecutionStates | null): string {
  if (!s) return 'Execution state: checking…';
  return [s.dhan, s.delta].map((v) => `${venueName(v)} ${SHORT[v.effective.state]}${v.effective.state === 'LIVE' ? '' : ` (${v.effective.reasons[0] ?? 'blocked'})`}`).join(' · ');
}

/** One sentence about the venue's activity today. A blocked attempt is not a sent order, and the two are never merged. */
export function todayLine(v: VenueState): string {
  const t = v.today;
  return `Today: ${t.sent_to_broker} sent to ${venueName(v)}${t.sent_to_broker ? ` (${t.rejected_by_broker} rejected by the broker)` : ''} · ${t.blocked_attempts} blocked attempt${t.blocked_attempts === 1 ? '' : 's'} (never sent)`;
}


/** One unambiguous line per forward test, derived only from the backend's effective state and the test's own live flag. */
export function testModeLine(v: VenueState | null | undefined, executeLive: boolean | undefined): string {
  const name = v ? venueName(v) : 'the broker';
  if (executeLive && v?.effective.state === 'LIVE') return `LIVE: orders are sent to ${name}`;
  if (executeLive && v) return `Paper: no orders sent (this test is flagged live, but ${name} is ${SHORT[v.effective.state]}: ${v.effective.reasons[0] ?? 'blocked'})`;
  return 'Paper: no orders sent';
}

/** One line for the whole account on this venue. */
export function accountLine(v: VenueState): string {
  const n = venueName(v);
  const s = v.effective.state;
  if (s === 'LIVE') return `Account: LIVE · ${n} orders can be sent now (${v.live_tests} test${v.live_tests === 1 ? '' : 's'} flagged live)`;
  return `Account: ${s === 'OFF' ? 'OFF' : 'ARMED but blocked'} · no orders are sent to ${n} (${v.effective.reasons[0] ?? 'blocked'})`;
}
