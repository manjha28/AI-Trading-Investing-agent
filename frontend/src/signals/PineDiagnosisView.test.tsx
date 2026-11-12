// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PineDiagnosisView } from './PineDiagnosisView';
import type { PineReport } from './types';

afterEach(cleanup);

// Test data only: the shape the backend returns for a stateful script (var + loop, with cascades).
const errors = [
  { line: 5, message: "'var' is not supported", cause: 'state' },
  { line: 6, message: "'var' is not supported", cause: 'state' },
  { line: 9, message: "'for' is not supported", cause: 'loops' },
  { line: 13, message: "unknown or unsupported identifier 'cum'", cause: 'identifier', follow_on_of: 'state' },
  { line: 14, message: "unknown or unsupported identifier 'score'", cause: 'identifier', follow_on_of: 'state' },
  { line: 15, message: 'unexpected indentation (only `if` blocks are supported)', cause: 'blocks', follow_on_of: 'loops' },
];
const limits = ["Only the decision timeframe's OHLCV is visible.", 'Only indicators in the trusted library registry are available.', 'Output is four boolean signals: no stop or limit orders.', 'Each run has a 20 second / 512 MB budget.'];
const pine = {
  filename: 't.pine', errors,
  diagnosis: {
    verdict: 'Cannot import: needs var / persistent state and loops (for / while). Not expressible in the rule DSL; use the Python strategy path.',
    total_errors: 6, root_cause_count: 3, follow_on_count: 3,
    groups: [
      { key: 'state', label: 'var / persistent state', blocking: true, count: 2, follow_on_count: 2, example_line: 5, example_message: "'var' is not supported" },
      { key: 'loops', label: 'loops (for / while)', blocking: true, count: 1, follow_on_count: 1, example_line: 9, example_message: "'for' is not supported" },
    ],
    route: { to: 'python' as const, reason: 'The rule DSL cannot express persistent state, arrays, loops or user-defined functions.', limits },
  },
} as unknown as PineReport;

describe('PineDiagnosisView', () => {
  it('leads with one verdict and counts root causes against total errors', () => {
    render(<PineDiagnosisView pine={pine} onUsePython={() => undefined} />);
    expect(screen.getByText(/Cannot import: needs var \/ persistent state and loops/)).toBeTruthy();
    expect(screen.getByText(/3 root causes, plus 3 follow-on errors caused by them \(6 errors in total\)/)).toBeTruthy();
  });

  it('groups by cause with a count and an example line, expanding only the first group, and collapses follow-ons', () => {
    const { container } = render(<PineDiagnosisView pine={pine} onUsePython={() => undefined} />);
    expect(screen.getByText(/2 lines, e\.g\. line 5/)).toBeTruthy();
    expect(screen.getByText(/2 follow-on errors/)).toBeTruthy();
    const groups = container.querySelectorAll('details.sl-issue-group');
    expect([...groups].map((g) => g.hasAttribute('open'))).toEqual([true, false]);
    // follow-ons are not listed as causes: only the 2 root 'var' lines appear in the first group
    expect(groups[0].querySelectorAll('li')).toHaveLength(2);
  });

  it('routes blocking constructs to the Python path and states its limits up front', () => {
    const open = vi.fn();
    render(<PineDiagnosisView pine={pine} onUsePython={open} />);
    expect(screen.getByText(/20 second \/ 512 MB/)).toBeTruthy();
    expect(screen.getByText(/no stop or limit orders/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /Open the Python strategy path and template/ }));
    expect(open).toHaveBeenCalledOnce();
  });

  it('shows no Python route when nothing blocking was found', () => {
    const light = { ...pine, diagnosis: { ...pine.diagnosis!, route: null } } as PineReport;
    render(<PineDiagnosisView pine={light} onUsePython={() => undefined} />);
    expect(screen.queryByRole('button', { name: /Python strategy path/ })).toBeNull();
  });
});
