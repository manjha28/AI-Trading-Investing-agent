import { describe, expect, it } from 'vitest';
import {
  BASIS_DELAYED,
  BASIS_LIVE,
  BASIS_SESSION_CLOSE,
  BASIS_UNAVAILABLE,
  basisForFreshnessStatus,
  basisLabel,
  deriveMarketDataStatus,
} from './priceStates';

describe('deriveMarketDataStatus', () => {
  it('returns unknown while nothing is loaded, so no placeholder status is shown', () => {
    expect(deriveMarketDataStatus(null)).toBe('unknown');
    expect(deriveMarketDataStatus(undefined)).toBe('unknown');
  });

  it('treats a session_close-only portfolio as session_close, not live', () => {
    expect(
      deriveMarketDataStatus([
        { freshness: 'session_close', basis: BASIS_SESSION_CLOSE },
        { freshness: 'session_close', basis: BASIS_SESSION_CLOSE },
      ]),
    ).toBe('session_close');
  });

  it('returns live only when every row has a live basis', () => {
    expect(deriveMarketDataStatus([{ freshness: 'live', basis: BASIS_LIVE }])).toBe('live');
  });

  it('returns unavailable when any holding has no usable basis', () => {
    expect(
      deriveMarketDataStatus([
        { freshness: 'live', basis: BASIS_LIVE },
        { freshness: 'unavailable', basis: BASIS_UNAVAILABLE },
      ]),
    ).toBe('unavailable');
  });

  it('returns unavailable for an empty portfolio read', () => {
    expect(deriveMarketDataStatus([])).toBe('unavailable');
  });

  it('gives one value for the same rows whether read by the header or the sidebar', () => {
    const rows = [{ freshness: 'session_close', basis: BASIS_SESSION_CLOSE }];
    expect(deriveMarketDataStatus(rows)).toBe(deriveMarketDataStatus([...rows]));
  });
});

describe('basisForFreshnessStatus', () => {
  it('maps only live, delayed and session_close freshness to a valuation basis', () => {
    expect(basisForFreshnessStatus('live')).toBe(BASIS_LIVE);
    expect(basisForFreshnessStatus('session_close')).toBe(BASIS_SESSION_CLOSE);
    expect(basisForFreshnessStatus('delayed')).toBe(BASIS_DELAYED);
    expect(basisForFreshnessStatus('historical')).toBeNull();
  });
});

describe('basisLabel', () => {
  it('labels a missing basis as Unavailable rather than a number', () => {
    expect(basisLabel(null)).toBe('Unavailable');
    expect(basisLabel(BASIS_SESSION_CLOSE)).toBe('Session close');
  });
});
