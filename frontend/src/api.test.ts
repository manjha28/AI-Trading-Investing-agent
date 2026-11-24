import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiRequest } from './api';

afterEach(() => vi.unstubAllGlobals());

describe('apiRequest de-duplication', () => {
  it('shares one in-flight GET, but a write makes the next read go to the server instead of joining an older request', async () => {
    const urls: string[] = [];
    let release: (r: Response) => void = () => undefined;
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      urls.push(`${init?.method ?? 'GET'} ${url}`);
      if ((init?.method ?? 'GET') === 'GET' && urls.filter((u) => u.startsWith('GET')).length === 1) return new Promise<Response>((r) => { release = r; });   // the first GET stays pending
      if (init?.method === 'DELETE') return Promise.resolve({ ok: true, status: 204 } as Response);
      return Promise.resolve(new Response(JSON.stringify(['fresh']), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    }));
    const first = apiRequest('/alerts/rules');
    const joined = apiRequest('/alerts/rules');                       // same read while the first is pending: shared
    expect(urls.filter((u) => u.startsWith('GET')).length).toBe(1);
    await apiRequest('/alerts/rules/1', { method: 'DELETE' });
    const after = await apiRequest('/alerts/rules');                  // after the write: a NEW request, not the stale pending one
    expect(urls.filter((u) => u.startsWith('GET')).length).toBe(2);
    expect(after).toEqual(['fresh']);
    release(new Response(JSON.stringify(['stale']), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    expect(await first).toEqual(['stale']); expect(await joined).toEqual(['stale']);
  });
});
