import { useState } from 'react';
import { lab, messageOf } from './lab';
import { Field, Notice, Pill, Scroll, StatusPill } from './shared';
import { PineDiagnosisView } from './PineDiagnosisView';
import type { Definition, Meta, PineReport, PythonReport } from './types';

export function ImportPanel({ meta, onDraft }: { meta: Meta; onDraft: (d: Definition, sourceText: string, note: string) => void }) {
  const [kind, setKind] = useState<'pine' | 'python'>('pine');
  const [market, setMarket] = useState('equity_in');
  const [tf, setTf] = useState('5m');
  const [file, setFile] = useState<{ name: string; text: string } | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pine, setPine] = useState<PineReport | null>(null);
  const [py, setPy] = useState<PythonReport | null>(null);
  const [ack, setAck] = useState(false);
  const [showTemplate, setShowTemplate] = useState(false);

  const accept = kind === 'pine' ? ['.pine', '.txt'] : ['.py'];
  const onFile = async (f: File | undefined) => {
    setPine(null); setPy(null); setAck(false); setError(null); setFileError(null);
    if (!f) { setFile(null); return; }
    if (!accept.some((ext) => f.name.toLowerCase().endsWith(ext))) { setFile(null); setFileError(`Unsupported file type. Choose ${accept.join(' or ')}.`); return; }
    if (f.size > 200_000) { setFile(null); setFileError('File is larger than 200 KB.'); return; }
    setFile({ name: f.name, text: await f.text() });
  };
  const analyze = async () => {
    if (!file) return;
    setBusy(true); setError(null); setAck(false);
    try {
      if (kind === 'pine') { setPy(null); setPine(await lab<PineReport>('/import/pine', { method: 'POST', body: { filename: file.name, text: file.text, market, decision_timeframe: tf } })); }
      else { setPine(null); setPy(await lab<PythonReport>('/import/python', { method: 'POST', body: { filename: file.name, text: file.text, market } })); }
    } catch (e) { setError(messageOf(e)); } finally { setBusy(false); }
  };
  const report = kind === 'pine' ? pine : py;
  const importable = !!report?.can_import && !!report.definition;
  const needsAck = kind === 'pine' ? pine?.compatibility === 'partial' : true;

  return (
    <div className="sl-import">
      <nav className="sl-subtabs" aria-label="Import type">
        <button type="button" className={kind === 'pine' ? 'active' : ''} aria-pressed={kind === 'pine'} onClick={() => { setKind('pine'); setFile(null); setPine(null); setPy(null); }}>Pine Script</button>
        <button type="button" className={kind === 'python' ? 'active' : ''} aria-pressed={kind === 'python'} onClick={() => { setKind('python'); setFile(null); setPine(null); setPy(null); }}>Python</button>
      </nav>
      <div className="sl-grid-2">
        <section className="sl-card">
          <h3>{kind === 'pine' ? 'Import Pine Script (v5 subset)' : 'Import a Python strategy'}</h3>
          <p className="sl-muted">{kind === 'pine'
            ? 'A bounded subset is translated into an editable strategy. Anything outside it is reported with its line number and blocks the import. Imports are never claimed to match TradingView.'
            : 'The file is screened statically and run in an isolated worker with time, memory and CPU limits. It sees only OHLCV and indicator series you declare.'}</p>
          <div className="sl-row">
            <Field label={`File (${accept.join(', ')})`}><input type="file" accept={accept.join(',')} aria-label="Strategy file" onChange={(e) => onFile(e.target.files?.[0])} /></Field>
            <Field label="Market"><select value={market} onChange={(e) => setMarket(e.target.value)}><option value="equity_in">Indian equity</option></select></Field>
            {kind === 'pine' && <Field label="Chart timeframe" hint="Pine series use this timeframe"><select value={tf} onChange={(e) => setTf(e.target.value)}>{meta.timeframes.map((t) => <option key={t}>{t}</option>)}</select></Field>}
          </div>
          {fileError && <Notice tone="bad">{fileError}</Notice>}
          {file && <p className="sl-muted">Selected <code>{file.name}</code> · {file.text.length.toLocaleString()} characters</p>}
          <div className="sl-row">
            <button type="button" className="sl-primary" disabled={!file || busy} onClick={analyze}>{busy ? 'Analyzing…' : 'Analyze compatibility'}</button>
            {kind === 'python' && <button type="button" onClick={() => setShowTemplate(!showTemplate)}>{showTemplate ? 'Hide' : 'Show'} template &amp; interface</button>}
          </div>
          {error && <Notice tone="bad">{error}</Notice>}
          {kind === 'python' && showTemplate && <pre className="sl-code" aria-label="Python template">{meta.python.template}</pre>}
        </section>
        <section className="sl-card">
          <h3>{kind === 'pine' ? 'Supported Pine subset' : 'Sandbox model'}</h3>
          {kind === 'pine' ? (
            <div className="sl-support">
              <h4><Pill tone="ok">supported</Pill></h4><ul>{meta.pine.supported.map((x) => <li key={x}>{x}</li>)}</ul>
              <h4><Pill tone="warn">partial</Pill></h4><ul>{meta.pine.partial.map((x) => <li key={x}>{x}</li>)}</ul>
              <h4><Pill tone="bad">unsupported</Pill></h4><ul>{meta.pine.unsupported.map((x) => <li key={x}>{x}</li>)}</ul>
            </div>
          ) : (
            <ul className="sl-support">
              <li>{meta.python.isolation}</li><li>Allowed imports: {meta.python.allowed_imports.join(', ')}</li>
              <li>Limits: {meta.python.timeout_seconds}s CPU/wall time, {meta.python.memory_mb} MB memory, no file writes, no network, no environment or secrets.</li>
              <li>History is exposed through read-only views: the current bar and earlier bars only.</li>
            </ul>
          )}
        </section>
      </div>

      {report && (
        <section className="sl-card" aria-label="Compatibility report">
          <header className="sl-card-head"><h3>Compatibility report — {report.filename}</h3>
            {pine ? <StatusPill status={pine.compatibility} /> : <StatusPill status={py?.can_import ? 'full' : 'unsupported'} />}</header>
          {pine && <Notice tone="warn">{pine.equivalence_note}</Notice>}
          {py && <Notice tone="info">{py.isolation}</Notice>}
          {pine && pine.errors.length > 0 && <PineDiagnosisView pine={pine} onUsePython={() => { setKind('python'); setFile(null); setPine(null); setPy(null); setShowTemplate(true); }} />}
          {py && py.screen_errors.length > 0 && <><h4>Rejected ({py.screen_errors.length})</h4><ul className="sl-issues">{py.screen_errors.map((e, i) => <li key={i} className="sl-issue-error"><code>{e.line ? `line ${e.line}` : 'file'}</code> {e.message}</li>)}</ul></>}
          {pine && pine.items.length > 0 && (
            <Scroll label="Construct analysis"><table className="sl-table sl-compact"><thead><tr><th>Line</th><th>Status</th><th>Construct</th><th>Note</th></tr></thead><tbody>
              {pine.items.map((it, i) => <tr key={i}><td className="sl-r">{it.line}</td><td><StatusPill status={it.status} /></td><td><code>{it.construct}</code></td><td>{it.message}</td></tr>)}
            </tbody></table></Scroll>
          )}
          {report.definition && (
            <div className="sl-preview" aria-label="Translated strategy preview">
              <h4>Translated strategy</h4>
              <dl className="sl-dl">
                <dt>Name</dt><dd>{report.definition.name}</dd><dt>Market</dt><dd>{report.definition.market}</dd><dt>Decision timeframe</dt><dd>{report.definition.decision_timeframe}</dd>
                <dt>Indicators</dt><dd>{report.definition.indicators.map((i) => `${i.indicator}(${Object.values(i.params).join(',')})@${i.timeframe}`).join(', ') || 'none'}</dd>
                <dt>Entries</dt><dd>{[report.definition.long_entry && 'long', report.definition.short_entry && 'short'].filter(Boolean).join(' + ') || 'none'}</dd>
                <dt>Exits</dt><dd>{[report.definition.long_exit && 'long exit', report.definition.short_exit && 'short exit'].filter(Boolean).join(' + ') || 'simulation exits only'}</dd>
              </dl>
              <details><summary>Show translated definition (JSON)</summary><pre className="sl-code">{JSON.stringify(report.definition, null, 2)}</pre></details>
            </div>
          )}
          {importable && (
            <div className="sl-confirm">
              {needsAck && <label className="sl-check"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> {kind === 'pine' ? 'I understand partial items are approximated or ignored and the result may differ from TradingView.' : 'I understand this file will run in a best-effort sandbox that is not a container boundary.'}</label>}
              <button type="button" className="sl-primary" disabled={needsAck && !ack} onClick={() => file && report.definition && onDraft(report.definition, file.text, `Imported ${file.name}. Choose instruments, review, then save.`)}>Review in Strategy Builder</button>
            </div>
          )}
          {!importable && <Notice tone="bad">This file cannot be imported. Fix the listed items and analyze again; nothing was saved.</Notice>}
        </section>
      )}
    </div>
  );
}
