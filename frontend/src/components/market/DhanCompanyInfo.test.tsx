import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { buildCompanyMetrics, DhanCompanyInfo, type CompanyInfoDetails, type CompanyInfoField } from './DhanCompanyInfo';

afterEach(cleanup);

const f = (key: string, value: string | number | null, over: Partial<CompanyInfoField> = {}): CompanyInfoField => ({
  key, label: key, value, presence: value === null ? 'null' : value === '' ? 'empty_string' : 'populated', unit: 'x', audit_status: 'verified',
  audit_text: 'Verified - used by Research', note: 'Internal backend note.', ...over,
});
const info = (co: CompanyInfoField[], ratios: CompanyInfoField[] = [], shp: CompanyInfoField[] = []): CompanyInfoDetails => ({
  source: 'Dhan Company Info', retrieved_at: '2026-11-05T05:00:00+00:00', reporting_period: null, reporting_period_note: 'Not provided by source',
  statement_basis: 'Not provided by source', warnings: ['Every value is an undated provider snapshot.'],
  groups: [{ key: 'CO', label: 'Company overview', returned: true, fields: co }, { key: 'RATIOS', label: 'Ratios', returned: true, fields: ratios },
           { key: 'SHP', label: 'Shareholding', returned: true, fields: shp }],
});
// Values are those Dhan really returned for RELIANCE during the audit.
const reliance = () => info(
  [f('SECTOR', 'Energy'), f('INDUSTRY', 'Petroleum Products'), f('MARKET_CAP', 1778175.5880708, { inr_value: '17781755880708' }), f('BOOK_VALUE', 697.88), f('EPS', 59.69),
   f('DIVIDEND_YIELD', 0.46), f('EBITDA', 207911), f('FIFTY_TWO_WEEK_HIGH', 1611.8), f('FIFTY_TWO_WEEK_LOW', 1152.2)],
  [f('PE_RATIO', 18.6), f('PB_RATIO', 1.88), f('ROCE', 9.18), f('DEBT_TO_EQUITY', 0.44), f('ROE', null), f('INDUSTRY_PB_RATIO', 1.17), f('INDUSTRY_OPERATING_MARGIN', 10.13), f('PE_TO_SECTOR_PE', 1.44)],
  [f('PROMOTER_HOLDING', 50.44), f('FII_HOLDING', 16.67), f('DII_HOLDING', 21.72), f('PUBLIC_HOLDING', 8.67), f('REPORTING_PERIOD', null), f('CHANGE_IN_FII_HOLDING', -0.53)],
);

describe('DhanCompanyInfo (user-facing metrics)', () => {
  it('groups metrics into Company overview, Valuation & ratios, Shareholding and Price context with readable names and units', () => {
    render(<DhanCompanyInfo info={reliance()} />);
    const section = (name: string) => screen.getByRole('region', { name });
    const overview = within(section('Company overview'));
    expect(overview.getByText('Sector').nextSibling).toHaveTextContent('Energy');
    expect(overview.getByText('Market capitalisation').nextSibling).toHaveTextContent('₹17.78 Lakh Cr');
    expect(overview.getByText('Book value per share').nextSibling).toHaveTextContent('₹697.88');
    expect(overview.getByText('Earnings per share').nextSibling).toHaveTextContent('₹59.69');
    expect(overview.getByText('Dividend yield').nextSibling).toHaveTextContent('0.46%');
    const valuation = within(section('Valuation & ratios'));
    expect(valuation.getByText('P/E').nextSibling).toHaveTextContent('18.60x');
    expect(valuation.getByText('P/B').nextSibling).toHaveTextContent('1.88x');
    expect(valuation.getByText('EBITDA').nextSibling).toHaveTextContent('₹2.08 Lakh Cr');
    expect(valuation.getByText('ROCE').nextSibling).toHaveTextContent('9.18%');
    expect(valuation.getByText('Debt to equity').nextSibling).toHaveTextContent('0.44x');
    const holdings = within(section('Shareholding'));
    expect(holdings.getByText('Promoter').nextSibling).toHaveTextContent('50.44%');
    expect(holdings.getByText('FII').nextSibling).toHaveTextContent('16.67%');
    expect(holdings.getByText('DII').nextSibling).toHaveTextContent('21.72%');
    expect(holdings.getByText('Public').nextSibling).toHaveTextContent('8.67%');
    const price = within(section('Price context'));
    expect(price.getByText('52-week high').nextSibling).toHaveTextContent('₹1,611.80');
    expect(price.getByText('52-week low').nextSibling).toHaveTextContent('₹1,152.20');
  });

  it('shows no diagnostic machinery: no badges, verification text, notes columns, raw API keys or tables', () => {
    const { container } = render(<DhanCompanyInfo info={reliance()} />);
    expect(container.textContent).not.toMatch(/verified|unverified|used by research|needs validation|unreliable|shown only/i);
    expect(container.textContent).not.toMatch(/MARKET_CAP|BOOK_VALUE|PB_RATIO|PE_RATIO|PROMOTER_HOLDING|Internal backend note/);
    expect(container.querySelector('table, thead, th, code')).toBeNull();
  });

  it('displays only meaningful values: null, empty, missing and zero-as-unknown fields are omitted, never shown as 0 or a placeholder', () => {
    const data = info([f('SECTOR', ''), f('INDUSTRY', null), f('MARKET_CAP', null), f('BOOK_VALUE', null, { presence: 'missing' }), f('EPS', null), f('DIVIDEND_YIELD', 0)],
      [f('PE_RATIO', null), f('PB_RATIO', 2.2), f('ROE', null)], [f('PROMOTER_HOLDING', null), f('PUBLIC_HOLDING', 12.5)]);
    const { container } = render(<DhanCompanyInfo info={data} />);
    expect(screen.queryByRole('region', { name: 'Company overview' })).toBeNull();  // nothing meaningful in it
    expect(screen.queryByRole('region', { name: 'Price context' })).toBeNull();
    expect(within(screen.getByRole('region', { name: 'Valuation & ratios' })).getAllByRole('article')).toHaveLength(1);
    expect(within(screen.getByRole('region', { name: 'Shareholding' })).getAllByRole('article')).toHaveLength(1);
    expect(container.textContent).not.toMatch(/unavailable|n\/a|null|0\.00%|₹0\.00|—/i);
  });

  it('renders nothing at all when Dhan returned no meaningful values', () => {
    const { container } = render(<DhanCompanyInfo info={info([f('SECTOR', ''), f('MARKET_CAP', null)], [f('ROE', null)], [f('REPORTING_PERIOD', null)])} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('keeps caveats to one short note per section, not beside every metric', () => {
    render(<DhanCompanyInfo info={reliance()} />);
    expect(screen.getAllByText(/does not provide a reporting period/)).toHaveLength(1);
    expect(screen.getAllByText(/may not total 100%/)).toHaveLength(1);
    expect(screen.getAllByText(/as reported by Dhan/).length).toBe(2);  // overview EPS note + valuation note, once each
    expect(screen.getAllByText(/Source: Dhan/)).toHaveLength(1);
    expect(screen.getByText(/Retrieved .*2026.* IST/)).toBeInTheDocument();
  });

  it('never presents unreliable data: industry benchmarks, P/E vs sector, share-holding changes and the reporting period are not shown', () => {
    const { container } = render(<DhanCompanyInfo info={reliance()} />);
    expect(container.textContent).not.toMatch(/industry (p\/b|average|operating|eps)|sector p\/e|change in|reporting period:/i);
    expect(container.textContent).not.toContain('1.17');
    expect(container.textContent).not.toContain('10.13');
  });

  it('handles negative earnings, banks with a zero debt ratio, and genuine zero debt', () => {
    const loss = buildCompanyMetrics(info([f('SECTOR', 'Consumer Services')], [f('PE_RATIO', -40.8), f('PB_RATIO', 2.18), f('DEBT_TO_EQUITY', 0.05)])).valuation;
    expect(loss.find((m) => m.label === 'P/E')?.value).toBe('Not meaningful');  // never "-40.80x"
    const bank = buildCompanyMetrics(info([f('SECTOR', 'Financial Services')], [f('DEBT_TO_EQUITY', 0), f('PB_RATIO', 1.42)])).valuation;
    expect(bank.find((m) => m.label === 'Debt to equity')).toBeUndefined();  // 0 means "not applicable" for a bank
    const debtFree = buildCompanyMetrics(info([f('SECTOR', 'Healthcare')], [f('DEBT_TO_EQUITY', 0)])).valuation;
    expect(debtFree.find((m) => m.label === 'Debt to equity')?.value).toBe('0.00x');  // a real zero for a non-financial
  });

  it('handles long names, very large market caps and out-of-range holdings', () => {
    const longName = 'Pharmaceuticals, Biotechnology & Diversified Specialty Chemicals and Allied Industrial Products';
    const { container } = render(<DhanCompanyInfo info={info(
      [f('SECTOR', 'Healthcare'), f('INDUSTRY', longName), f('MARKET_CAP', 9_999_999.99, { inr_value: '55555500000000' })], [],
      [f('PROMOTER_HOLDING', 140), f('FII_HOLDING', -3), f('DII_HOLDING', 33.3)])} />);
    expect(screen.getByText(longName)).toBeInTheDocument();
    expect(screen.getByText('₹55.56 Lakh Cr')).toBeInTheDocument();
    const holdings = within(screen.getByRole('region', { name: 'Shareholding' }));
    expect(holdings.getAllByRole('article')).toHaveLength(1);  // 140% and -3% are discarded, not displayed
    expect(container.querySelector('.company-metrics-bar')).toHaveAttribute('aria-label', 'Shareholding split: DII 33.30%');
  });

  it('draws the shareholding split as a labelled bar whose segments match the percentages', () => {
    const { container } = render(<DhanCompanyInfo info={reliance()} />);
    const bar = container.querySelector('.company-metrics-bar') as HTMLElement;
    expect(bar).toHaveAttribute('role', 'img');
    expect([...bar.children].map((el) => (el as HTMLElement).style.width)).toEqual(['50.44%', '16.67%', '21.72%', '8.67%']);
  });

  it('reuses the existing card grid so it inherits the Market Data theme and responsive columns', () => {
    const { container } = render(<DhanCompanyInfo info={reliance()} />);
    expect(container.querySelectorAll('.fundamental-snapshot-grid')).toHaveLength(4);
    expect(container.querySelectorAll('.fundamental-snapshot-grid > article > strong').length).toBeGreaterThan(10);
  });
});
