import { describe, expect, it } from 'vitest';
import { exitMarker } from './CryptoCharts';

describe('exitMarker (crypto backtest exits)', () => {
  it('labels stops SL, targets T and signal reversals X so a flip is never a blank circle', () => {
    expect(exitMarker('stop')).toEqual({ text: 'SL', outcome: 'stop' });
    expect(exitMarker('target')).toEqual({ text: 'T', outcome: 'target' });
    expect(exitMarker('signal')).toEqual({ text: 'X', outcome: 'signal' });
    expect(exitMarker('data_gap').text).toBe('gap');
    expect(exitMarker('end_of_data').text).toBe('end');
  });
});
