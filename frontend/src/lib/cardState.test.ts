import { describe, expect, it } from 'vitest';
import { bandText, cardState, rLabel } from './cardState';

const lv = (over: object = {}) => ({ status: 'available', entry_zone: { low: '100.00', high: '103.00' }, reference_close: '101.50', stop: '95.00', targets: [{ label: '2R', price: '115.00', risk_reward: 2 }], ...over });

describe('cardState', () => {
  it('is in band when the price is inside the entry zone, edges included', () => {
    expect(cardState(lv()).kind).toBe('in_band');
    expect(cardState(lv(), '100').kind).toBe('in_band');
    expect(cardState(lv(), '103').tone).toBe('positive');
  });
  it('says WHY it waits when the price is above or below the band, never a bare "Wait for entry"', () => {
    const above = cardState(lv(), '108');
    expect(above.kind).toBe('above_band'); expect(above.label).toContain('Wait:'); expect(above.reason).toContain('above the band ₹100.00–₹103.00');
    const below = cardState(lv(), '97');
    expect(below.kind).toBe('below_band'); expect(below.reason).toContain('below the band');
    expect(cardState(lv({ entry_label: 'Wait for a pullback into 100.00-103.00' }), '108').label).toBe('Wait for a pullback into 100.00-103.00');   // the engine's own wording wins when it sends one
  });
  it('flags a setup whose first target is already below the price, before the band check', () => {
    const s = cardState(lv({ targets: [{ label: '2R', price: '105.00' }] }), '106');
    expect(s.kind).toBe('target_below_close'); expect(s.label).toContain('first target ₹105.00');
    expect(cardState(lv({ targets: [{ label: '2R', price: '101.50' }] })).kind).toBe('target_below_close');      // equal counts as passed
  });
  it('treats missing levels or a missing price as unknown, not as a favourable state', () => {
    expect(cardState(null).kind).toBe('no_levels');
    expect(cardState({ status: 'unavailable', reason: 'not enough candles' }).label).toBe('No levels: not enough candles');
    expect(cardState(lv({ reference_close: undefined })).kind).toBe('no_price');
    expect(cardState(lv({ entry_zone: undefined })).kind).toBe('no_price');
  });
  it('formats the band and the R label, with dashes when absent', () => {
    expect(bandText(lv())).toBe('₹100.00–₹103.00'); expect(bandText(null)).toBe('—');
    expect(rLabel(lv())).toBe('2R'); expect(rLabel(lv({ targets: [{ label: 'T1', price: '1', risk_reward: 2.5 }] }))).toBe('T1 · 2.5R'); expect(rLabel(lv({ targets: [] }))).toBe('—');
  });
});
