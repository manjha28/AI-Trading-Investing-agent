import { ApiRequestError } from '../api';

export function messageOf(error: unknown): string {
  if (error instanceof ApiRequestError || error instanceof Error) return error.message;
  return 'Request failed.';
}

export type Market = {
  product_id: string;
  symbol: string;
  state: string;
  quote_time: string | null;
  last_price: string | null;
  mark_price: string | null;
  volume_24h: string | null;
};

export type Candles = {
  symbol: string;
  resolution: string;
  source: string;
  count: number;
  gaps: number;
  candles: { timestamp: string; open: string; high: string; low: string; close: string; volume: string }[];
};

export type ContractInfo = {
  symbol: string;
  contract_value: string;
  tick_size: string | null;
  taker_fee_rate: string;
  maker_fee_rate: string;
  quote_currency: string;
  source: string;
  /** Underlying units in one contract (the lot size), e.g. 0.001 BTC. */
  units_per_contract?: string;
  initial_margin_pct?: string | null;
  maintenance_margin_pct?: string | null;
  max_leverage?: string | null;
  default_leverage?: string | null;
  liquidation_penalty_factor?: string | null;
  position_size_limit?: number | null;
};

export const RESOLUTIONS = ['1m', '5m', '15m', '1h', '4h', '1d'] as const;
/** Default candle history to download per resolution (the server enforces its own maximum). */
export const DEFAULT_DAYS: Record<string, number> = { '1m': 3, '5m': 14, '15m': 30, '1h': 90, '4h': 180, '1d': 365 };

/** Display a price at the contract's tick precision (display only; the API supplies every figure). */
export function atTick(value: string | null, tickSize: string | null | undefined): string {
  if (value === null) return '—';
  const decimals = tickSize && tickSize.includes('.') ? tickSize.replace(/0+$/, '').split('.')[1]?.length ?? 0 : 0;
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(decimals) : '—';
}
