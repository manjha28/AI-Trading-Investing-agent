/** One mapping from a domain word to a tone, so a status looks the same on every page. Add new words HERE, never as a one-off class. */
export type Tone = 'positive' | 'negative' | 'caution' | 'info' | 'neutral';

export const STATUS_TONE: Record<string, Tone> = {
  // trade verdicts and setup states
  buy: 'positive', hold: 'info', wait: 'caution', avoid: 'negative', radar: 'info',
  // data freshness and connection
  live: 'positive', stale: 'caution', delayed: 'caution', closed: 'neutral', unverified: 'caution', unknown: 'neutral',
  // multibagger classes
  compounder: 'positive', emerging: 'positive', watch: 'caution', not_a_fit: 'neutral', not_tradable: 'negative',
  multibagger_candidate: 'positive', quality_compounder: 'positive', new_listing: 'info', not_assessable: 'neutral',
};

export const STATUS_LABEL: Record<string, string> = {
  buy: 'Buy', hold: 'Hold', wait: 'Wait', avoid: 'Avoid', radar: 'Radar', live: 'Live', stale: 'Stale', delayed: 'Delayed', closed: 'Closed', unverified: 'Unverified', unknown: 'Unknown',
  compounder: 'Compounder', emerging: 'Emerging', watch: 'Watch', not_a_fit: 'Not a fit', not_tradable: 'Not tradable',
  multibagger_candidate: 'Candidate', quality_compounder: 'Compounder', new_listing: 'New listing', not_assessable: 'Not assessable',
};

export function toneOf(status: string | null | undefined): Tone {
  return (status && STATUS_TONE[status.toLowerCase().replace(/[\s-]+/g, '_')]) || 'neutral';
}
