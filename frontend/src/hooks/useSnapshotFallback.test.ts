import { describe, expect, it } from 'vitest';
import {
  SNAPSHOT_BACKOFF_MAX_MS,
  SNAPSHOT_REFRESH_MS,
  selectFreshestSource,
  snapshotRetryDelayMs,
} from './useSnapshotFallback';

describe('snapshotRetryDelayMs', () => {
  it('doubles the delay per consecutive failure and caps it', () => {
    expect(snapshotRetryDelayMs(1)).toBe(SNAPSHOT_REFRESH_MS);
    expect(snapshotRetryDelayMs(2)).toBe(SNAPSHOT_REFRESH_MS * 2);
    expect(snapshotRetryDelayMs(3)).toBe(SNAPSHOT_REFRESH_MS * 4);
    expect(snapshotRetryDelayMs(50)).toBe(SNAPSHOT_BACKOFF_MAX_MS);
  });
});

describe('selectFreshestSource', () => {
  const older = { observedAt: '2026-11-02T10:00:00.000Z' };
  const newer = { observedAt: '2026-11-02T10:05:00.000Z' };

  it('prefers live data when no snapshot exists', () => {
    expect(selectFreshestSource(older, null)).toBe('live');
  });

  it('never lets an older snapshot overwrite newer live data', () => {
    expect(selectFreshestSource(newer, older)).toBe('live');
  });

  it('keeps live data when timestamps are equal', () => {
    expect(selectFreshestSource(older, older)).toBe('live');
  });

  it('uses a snapshot only when it is newer than the live value', () => {
    expect(selectFreshestSource(older, newer)).toBe('snapshot');
  });

  it('falls back to a snapshot when there is no live value', () => {
    expect(selectFreshestSource(null, older)).toBe('snapshot');
    expect(selectFreshestSource(undefined, undefined)).toBe('none');
  });
});
