import { Chip } from './ui/kit';
import { SHORT, venueName, type VenueState } from '../lib/executionState';

/** The venue's effective state, with the reasons on hover. */
export function ExecutionStateChip({ venue }: { venue: VenueState | null | undefined }) {
  if (!venue) return <Chip tone="neutral" title="The server has not reported the execution state yet">Execution state unknown</Chip>;
  const s = venue.effective.state;
  return <Chip tone={s === 'LIVE' ? 'negative' : s === 'ARMED_BUT_BLOCKED' ? 'caution' : 'neutral'} title={venue.effective.reasons.join('; ') || 'Orders can be sent now'}>{venueName(venue)}: {SHORT[s]}</Chip>;
}
