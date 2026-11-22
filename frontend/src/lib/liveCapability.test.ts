import { describe, expect, it } from 'vitest';
import { LIVE_POSSIBLE, RESEARCH_ONLY, footerWording, sidebarWording } from './liveCapability';

describe('live-order wording follows the real capability', () => {
  it('says read-only only when live execution is off on the server', () => {
    expect(sidebarWording({ deployment_enabled: false })).toBe(RESEARCH_ONLY);
    expect(footerWording({ deployment_enabled: false })).toBe('No live orders');
  });
  it('says where live orders can come from when the deployment allows them, and whether they are armed', () => {
    expect(sidebarWording({ deployment_enabled: true, armed: false })).toBe(LIVE_POSSIBLE);
    expect(footerWording({ deployment_enabled: true, armed: false })).toContain('not armed');
    expect(footerWording({ deployment_enabled: true, armed: true })).toContain('(armed)');
  });
  it('does not claim "No live orders" while the capability is unknown', () => {
    expect(sidebarWording(null)).toBe(RESEARCH_ONLY);
    expect(footerWording(null)).not.toBe('No live orders');
  });
});
