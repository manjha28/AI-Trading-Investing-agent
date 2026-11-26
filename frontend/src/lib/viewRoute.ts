/** Each page has its own address (#/journal), so refresh, Back/Forward and bookmarks keep your place. */
export const VIEWS = ['overview', 'journal', 'market-data', 'brokers', 'alerts', 'corporate-actions', 'research', 'signals', 'crypto-delta', 'market-watch'] as const;
export type View = (typeof VIEWS)[number];

export function viewFromHash(hash: string): View | null {
  const name = hash.startsWith('#/') ? hash.slice(2) : '';
  if (!(VIEWS as readonly string[]).includes(name)) return null;
  return name === 'market-data' ? 'market-watch' : (name as View);      // a single instrument's page needs a selection, so a reload returns to the list
}
