import { describe, expect, it } from 'vitest';
import { depthStatusView, type DepthStatus } from './depthStatus';

describe('depthStatusView', () => {
  it('distinguishes every state with its own label', () => {
    const states: DepthStatus[] = ['connecting', 'subscribed', 'active', 'stale', 'reconnecting', 'no_data', 'unsupported', 'failed', 'market_closed', 'not_configured'];
    const labels = states.map((state) => depthStatusView(state, '', null).label);
    expect(new Set(labels).size).toBe(states.length);
  });
  it('only active is live, and subscribed explicitly says nothing is live yet', () => {
    expect(depthStatusView('active', '', null).tone).toBe('live');
    for (const state of ['subscribed', 'stale', 'reconnecting', 'no_data', 'connecting'] as DepthStatus[]) {
      expect(depthStatusView(state, '', null).tone).not.toBe('live');
    }
    expect(depthStatusView('subscribed', '', null).text).toMatch(/nothing is live yet/);
  });
  it('shows the actual provider reason instead of a generic message', () => {
    expect(depthStatusView('unsupported', 'Only NSE equity is supported.', null).text).toBe('Only NSE equity is supported.');
    expect(depthStatusView('failed', 'Dhan depth feed closed code=805 reason=too many connections', null).text).toMatch(/805/);
  });
});
