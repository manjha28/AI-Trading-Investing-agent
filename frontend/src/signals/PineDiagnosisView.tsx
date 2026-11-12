import type { PineReport } from './types';
import { Notice } from './shared';

/** One verdict, causes grouped with counts and an example, follow-on errors collapsed, the full list kept behind a fold. */
export function PineDiagnosisView({ pine, onUsePython }: { pine: PineReport; onUsePython: () => void }) {
  const d = pine.diagnosis;
  if (!d) {
    return <><h4>Errors ({pine.errors.length})</h4><ul className="sl-issues">{pine.errors.map((e, i) => <li key={i} className="sl-issue-error"><code>{e.line ? `line ${e.line}` : 'file'}</code> {e.message}</li>)}</ul></>;
  }
  return (
    <div aria-label="Import verdict">
      {d.verdict && <Notice tone="bad"><strong>{d.verdict}</strong></Notice>}
      <p className="sl-muted">{d.root_cause_count} root cause{d.root_cause_count === 1 ? '' : 's'}{d.follow_on_count > 0 ? `, plus ${d.follow_on_count} follow-on error${d.follow_on_count === 1 ? '' : 's'} caused by them` : ''} ({d.total_errors} errors in total).</p>
      {d.route && (
        <Notice tone="info">
          <p>{d.route.reason} This strategy needs the Python strategy path, where your logic runs as code.</p>
          <button type="button" onClick={onUsePython}>Open the Python strategy path and template</button>
          <p>Limits of the Python path, so they are known up front:</p>
          <ul>{d.route.limits.map((l) => <li key={l}>{l}</li>)}</ul>
        </Notice>
      )}
      <h4>Causes</h4>
      {d.groups.map((g, i) => (
        <details key={g.key} open={i === 0} className="sl-issue-group">
          <summary>
            <strong>{g.label}</strong> — {g.count} line{g.count === 1 ? '' : 's'}{g.example_line ? `, e.g. line ${g.example_line}` : ''}
            {g.follow_on_count > 0 ? ` · ${g.follow_on_count} follow-on error${g.follow_on_count === 1 ? '' : 's'}` : ''}
          </summary>
          <p className="sl-muted"><code>{g.example_message}</code>{g.blocking ? ' · cannot be expressed in the rule DSL' : ''}</p>
          <ul className="sl-issues">{pine.errors.filter((e) => (e.follow_on_of ?? e.cause) === g.key && !e.follow_on_of).map((e, n) => <li key={n} className="sl-issue-error"><code>{e.line ? `line ${e.line}` : 'file'}</code> {e.message}</li>)}</ul>
        </details>
      ))}
      <details>
        <summary>All {d.total_errors} errors, in file order</summary>
        <ul className="sl-issues">{pine.errors.map((e, i) => <li key={i} className={e.follow_on_of ? 'sl-issue-warn' : 'sl-issue-error'}><code>{e.line ? `line ${e.line}` : 'file'}</code> {e.message}{e.follow_on_of ? ' (follow-on)' : ''}</li>)}</ul>
      </details>
    </div>
  );
}
