import { useMemo, useState, type ReactNode } from 'react';

export function Panel({
  title,
  description,
  action,
  children,
  className = '',
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`coverage-panel ${className}`}>
      <div className="coverage-panel-content">
        <SectionHeading title={title} description={description} action={action} />
        {children}
      </div>
    </section>
  );
}

export function SectionHeading({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <header className="coverage-panel-heading">
      <div>
        <h2>{title}</h2>
        {description && <p>{description}</p>}
      </div>
      {action}
    </header>
  );
}

export type DataTableColumn<Row> = {
  key: string;
  label: string;
  render: (row: Row) => ReactNode;
  sortValue?: (row: Row) => string | number;
  align?: 'left' | 'right';
  className?: string;
};

export function DataTable<Row>({
  columns,
  rows,
  className = '',
  getRowKey,
}: {
  columns: DataTableColumn<Row>[];
  rows: Row[];
  className?: string;
  getRowKey?: (row: Row) => string;
}) {
  const [sort, setSort] = useState<{ key: string; direction: 'ascending' | 'descending' }>({
    key: '',
    direction: 'ascending',
  });
  const orderedRows = useMemo(() => {
    const column = columns.find((item) => item.key === sort.key && item.sortValue);
    if (!column?.sortValue) return rows;
    const value = column.sortValue;
    return [...rows].sort((left, right) => {
      const a = value(left);
      const b = value(right);
      const result = typeof a === 'number' && typeof b === 'number'
        ? a - b
        : String(a).localeCompare(String(b), undefined, { numeric: true });
      return sort.direction === 'ascending' ? result : -result;
    });
  }, [columns, rows, sort]);

  return (
    <div className="coverage-table-wrap">
      <table className={`coverage-table ${className}`}>
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                aria-sort={sort.key === column.key ? sort.direction : 'none'}
                className={column.align === 'right' ? 'coverage-numeric' : undefined}
              >
                {column.sortValue ? (
                  <button
                    type="button"
                    onClick={() => setSort((current) => ({
                      key: column.key,
                      direction: current.key === column.key && current.direction === 'ascending'
                        ? 'descending'
                        : 'ascending',
                    }))}
                  >
                    {column.label}
                  </button>
                ) : column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {orderedRows.map((row, index) => (
            <tr key={getRowKey?.(row) ?? index}>
              {columns.map((column) => (
                <td
                  key={column.key}
                  data-label={column.label}
                  className={[
                    column.align === 'right' ? 'coverage-numeric' : '',
                    column.className ?? '',
                  ].filter(Boolean).join(' ')}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function StatCard({
  label,
  value,
  detail,
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
}) {
  return (
    <article className="coverage-stat-card">
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </article>
  );
}

export function Badge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode;
  tone?: 'positive' | 'negative' | 'caution' | 'neutral' | 'unreliable';
}) {
  return <span className={`coverage-badge ${tone}`}>{children}</span>;
}

export function StatusDot({
  status,
  label,
}: {
  status: 'fresh' | 'stale' | 'failed' | 'never-synced';
  label: string;
}) {
  const tone = status === 'fresh'
    ? 'fresh'
    : status === 'stale'
      ? 'stale'
      : status === 'never-synced'
        ? 'never-synced'
        : 'failed';
  return (
    <span className="coverage-badge" aria-label={`Data status: ${label}`}>
      <span className={`coverage-status-dot ${tone}`} aria-hidden="true" />
      {label}
    </span>
  );
}

export function Skeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="coverage-skeleton" role="status" aria-label="Loading market data coverage">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="coverage-skeleton-row" />
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  message,
}: {
  title: string;
  message: string;
}) {
  return (
    <div className="coverage-state">
      <strong>{title}</strong>
      <p>{message}</p>
    </div>
  );
}

export function ErrorState({
  title,
  message,
  onRetry,
}: {
  title: string;
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="coverage-state error" role="alert">
      <strong>{title}</strong>
      <p>{message}</p>
      <button type="button" onClick={onRetry}>Retry</button>
    </div>
  );
}
