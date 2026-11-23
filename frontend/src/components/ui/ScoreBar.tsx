/** A 0-100 score as five segments, tone by value. Unknown renders "n/a" with the reason in the tooltip; it is never drawn as an empty (zero) bar. */
export function ScoreBar({ value, label, naReason = 'Not enough data to score' }: { value: number | null | undefined; label?: string; naReason?: string }) {
  if (value === null || value === undefined || Number.isNaN(value)) return <span className="ui-score ui-score-na" title={naReason} aria-label={`${label ? `${label}: ` : ''}not available. ${naReason}`}>n/a</span>;
  const v = Math.max(0, Math.min(100, value));
  const tone = v >= 70 ? 'positive' : v >= 40 ? 'caution' : 'negative';
  const filled = Math.max(v > 0 ? 1 : 0, Math.round(v / 20));
  return (
    <span className={`ui-score ui-tone-${tone}`} role="img" aria-label={`${label ? `${label}: ` : ''}${Math.round(v)} out of 100`} title={`${Math.round(v)} / 100`}>
      <span className="ui-score-segs" aria-hidden="true">{[0, 1, 2, 3, 4].map((i) => <i key={i} className={i < filled ? 'on' : ''} />)}</span>
      <span className="ui-score-num">{Math.round(v)}</span>
    </span>
  );
}
