import { formatMoney } from '../../lib/formatMoney';
import { formatIST } from '../../lib/formatIST';

/** Shape returned by the company-profile endpoint. The verification metadata stays in the payload (it is a backend
 *  data-quality concern); this view simply does not render it. */
export type CompanyInfoField = {
  key: string; label: string; value: string | number | null; presence: 'populated' | 'null' | 'empty_string' | 'missing' | 'group_not_returned';
  unit: string; audit_status: 'verified' | 'unvalidated' | 'unreliable'; audit_text: string; note: string; inr_value?: string | null;
};
export type CompanyInfoDetails = {
  source: string; retrieved_at: string; reporting_period: null; reporting_period_note: string; statement_basis: string; warnings: string[];
  groups: { key: string; label: string; returned: boolean; fields: CompanyInfoField[] }[];
};

type Metric = { label: string; value: string; dot?: string };

const number = (value: number, digits = 2) => value.toLocaleString('en-IN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
const rupees = (value: number) => `₹${number(value)}`;
const ratio = (value: number) => `${number(value)}x`;
const percent = (value: number) => `${number(value)}%`;

function raw(fields: Map<string, CompanyInfoField>, key: string): string | number | null {
  const field = fields.get(key);
  return field && field.presence === 'populated' ? field.value : null;
}
function numeric(fields: Map<string, CompanyInfoField>, key: string): number | null {
  const value = raw(fields, key);
  if (value === null || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}
function text(fields: Map<string, CompanyInfoField>, key: string): string | null {
  const value = raw(fields, key);
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

/** Builds the four display sections. A metric without a meaningful value is omitted, never shown as 0 or a placeholder.
 *  Industry benchmarks are not shown: Dhan returns zeros and negative or implausible values for them (e.g. for banks). */
export function buildCompanyMetrics(info: CompanyInfoDetails) {
  const fields = new Map<string, CompanyInfoField>(info.groups.flatMap((group) => group.fields.map((field) => [field.key, field] as const)));
  const sector = text(fields, 'SECTOR');
  const financialSector = /financ|bank|insur/i.test(sector ?? '');
  const marketCap = fields.get('MARKET_CAP')?.inr_value;
  const book = numeric(fields, 'BOOK_VALUE');
  const eps = numeric(fields, 'EPS');
  const dividend = numeric(fields, 'DIVIDEND_YIELD');
  const pe = numeric(fields, 'PE_RATIO');
  const pb = numeric(fields, 'PB_RATIO');
  const ebitda = numeric(fields, 'EBITDA');
  const roce = numeric(fields, 'ROCE');
  const debt = numeric(fields, 'DEBT_TO_EQUITY');
  const high = numeric(fields, 'FIFTY_TWO_WEEK_HIGH');
  const low = numeric(fields, 'FIFTY_TWO_WEEK_LOW');

  const overview: Metric[] = [];
  if (sector) overview.push({ label: 'Sector', value: sector });
  const industry = text(fields, 'INDUSTRY');
  if (industry) overview.push({ label: 'Industry', value: industry });
  if (marketCap) overview.push({ label: 'Market capitalisation', value: formatMoney(marketCap, 'INR') });
  if (book !== null) overview.push({ label: 'Book value per share', value: rupees(book) });
  if (eps !== null) overview.push({ label: 'Earnings per share', value: rupees(eps) });
  if (dividend !== null && dividend > 0) overview.push({ label: 'Dividend yield', value: percent(dividend) });

  const valuation: Metric[] = [];
  if (pe !== null) valuation.push({ label: 'P/E', value: pe > 0 ? ratio(pe) : 'Not meaningful' });
  if (pb !== null) valuation.push({ label: 'P/B', value: pb > 0 ? ratio(pb) : 'Not meaningful' });
  if (ebitda !== null) valuation.push({ label: 'EBITDA', value: formatMoney(String(ebitda * 10_000_000), 'INR') });
  if (roce !== null) valuation.push({ label: 'ROCE', value: percent(roce) });
  // A zero debt-to-equity for a bank or insurer means "not applicable", not "no debt".
  if (debt !== null && !(debt === 0 && financialSector)) valuation.push({ label: 'Debt to equity', value: ratio(debt) });

  const palette = ['#4c9be8', '#8e7cc3', '#3fd99a', '#e2bd68'];
  const holdings: Metric[] = [];
  for (const [index, [key, label]] of ([['PROMOTER_HOLDING', 'Promoter'], ['FII_HOLDING', 'FII'], ['DII_HOLDING', 'DII'], ['PUBLIC_HOLDING', 'Public']] as const).entries()) {
    const value = numeric(fields, key);
    if (value !== null && value >= 0 && value <= 100) holdings.push({ label, value: percent(value), dot: palette[index] });
  }
  const price: Metric[] = [];
  if (high !== null) price.push({ label: '52-week high', value: rupees(high) });
  if (low !== null) price.push({ label: '52-week low', value: rupees(low) });
  return { overview, valuation, holdings, price, pe };
}

function Section({ title, metrics, note, children }: { title: string; metrics: Metric[]; note?: string; children?: React.ReactNode }) {
  if (!metrics.length) return null;
  return <section className="company-metrics-section" aria-label={title}>
    <h4>{title}</h4>
    <div className="fundamental-snapshot-grid">
      {metrics.map((metric) => <article key={metric.label}>
        <span>{metric.dot && <i className="company-metrics-dot" style={{ background: metric.dot }} aria-hidden="true" />}{metric.label}</span>
        <strong>{metric.value}</strong>
      </article>)}
    </div>
    {children}
    {note && <p className="company-metrics-note">{note}</p>}
  </section>;
}

export function DhanCompanyInfo({ info }: { info: CompanyInfoDetails }) {
  const { overview, valuation, holdings, price } = buildCompanyMetrics(info);
  if (!overview.length && !valuation.length && !holdings.length && !price.length) return null;
  const shares = holdings.map((metric) => ({ ...metric, width: Math.max(0, Math.min(100, Number.parseFloat(metric.value.replace(/,/g, '')))) }));
  const retrieved = formatIST(info.retrieved_at);
  return <div className="company-metrics" aria-label="Company metrics">
    <Section title="Company overview" metrics={overview} note={overview.some((m) => m.label === 'Earnings per share') ? 'Earnings per share is as reported by Dhan; its period is not stated.' : undefined} />
    <Section title="Valuation & ratios" metrics={valuation} note="Ratios and EBITDA are as reported by Dhan without a stated period; EBITDA is shown in ₹ crore." />
    <Section title="Shareholding" metrics={holdings} note="Dhan does not provide a reporting period for shareholding, and the categories returned may not total 100%.">
      <div className="company-metrics-bar" role="img" aria-label={`Shareholding split: ${holdings.map((m) => `${m.label} ${m.value}`).join(', ')}`}>
        {shares.map((share) => <span key={share.label} style={{ width: `${share.width}%`, background: share.dot }} />)}
      </div>
    </Section>
    <Section title="Price context" metrics={price} />
    <p className="company-metrics-source">Source: Dhan · Retrieved {retrieved} IST</p>
  </div>;
}
