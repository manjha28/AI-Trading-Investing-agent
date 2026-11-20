export type Market = 'equity_in';
export type CompareOp = '>' | '>=' | '<' | '<=' | '==' | '!=' | 'crosses_above' | 'crosses_below';

export type IndOperand = { kind: 'indicator'; ref: string; output?: string | null; offset?: number };
export type PriceOperand = { kind: 'price'; field: string; timeframe: string; offset?: number };
export type ConstOperand = { kind: 'const'; value: number };
export type ArithOperand = { kind: 'arith'; op: '+' | '-' | '*' | '/'; left: Operand; right: Operand };
export type Operand = IndOperand | PriceOperand | ConstOperand | ArithOperand;

export type Compare = { type: 'compare'; left: Operand; op: CompareOp; right: Operand; within?: number; label?: string | null };
export type TimeWindow = { type: 'time_window'; start: string; end: string; label?: string | null };
export type Group = { type: 'group'; op: 'and' | 'or' | 'not'; children: Condition[]; label?: string | null };
export type Condition = Compare | TimeWindow | Group;

export type IndicatorDecl = { alias: string; indicator: string; params: Record<string, number | string>; timeframe: string };
export type RuleName = 'long_entry' | 'short_entry' | 'long_exit' | 'short_exit';

export type Definition = {
  schema_version: string;
  name: string;
  description: string;
  market: Market;
  universe: string[];
  base_timeframe: '1m';
  decision_timeframe: string;
  indicators: IndicatorDecl[];
  long_entry: Condition | null;
  short_entry: Condition | null;
  long_exit: Condition | null;
  short_exit: Condition | null;
  sizing: { method: string; value: number };
  stop: { mode: string; value: number; atr_length: number };
  target: { mode: string; value: number; atr_length: number };
  trail: { mode: string; value: number; atr_length: number };
  session: { no_entry_after: string | null; square_off: string | null };
  constraints: { allow_long: boolean; allow_short: boolean; max_trades_per_day: number | null };
  python: { source_sha256: string; entrypoint: string } | null;
  options?: OptionsSpec | null;
  source: { type: 'visual' | 'pine' | 'python' | 'legacy' | 'starter'; artifact: Record<string, unknown> | null; provenance: Record<string, unknown> };
};

export type OptionsSpec = {
  strategy_type: 'options_buying';
  underlying: { underlying: string; signal_source: 'futures' | 'spot'; spot_instrument_id: string | null; trend_timeframe: string; entry_timeframe: string; ema_fast: number; ema_slow: number; ema_pullback_alt: number;
    ema_slope_bars: number; adx_length: number; adx_min: number; rsi_length: number; rsi_long_min: number; rsi_short_max: number; atr_length: number; pullback_atr: number; pullback_lookback: number;
    volume_length: number; volume_multiple: number; max_candle_range_atr: number; max_vwap_extension_atr: number; swing_left: number; swing_right: number; start_time: string; last_entry_time: string };
  selection: { allow_ce: boolean; allow_pe: boolean; strike_choices: ('atm' | 'itm1')[]; delta_min: number; delta_max: number; delta_preferred: number; expiry_rank: number; allow_expiry_day: boolean;
    max_spread_pct: number; quote_max_age_seconds: number; min_volume: number; min_depth_lots: number; allow_calculated_greeks: boolean; risk_free_rate_pct: number };
  execution: { price_mode: 'quote_accurate' | 'indicative_ohlc'; premium_stop_pct: number; target_r: number; trailing_activation_r: number | null; trailing_distance_pct: number | null; max_hold_minutes: number;
    underlying_invalidation: boolean; opposite_signal_exit: boolean; thesis_exit: boolean; slippage_ticks: number; tick_size: number; entry_latency_seconds: number; max_entry_wait_seconds: number;
    ohlc_half_spread_pct: number; fee_schedule: string };
  risk: { risk_per_trade_pct: number; daily_loss_limit_pct: number; max_positions: number; sizing_method: 'risk_percent' | 'fixed_lots'; fixed_lots: number };
  event_blackouts: { start: string; end: string; label: string }[];
};
export type OptionsCoverage = {
  underlying: string; as_of: string; backtest_mode: 'quote_accurate' | 'indicative_ohlc' | 'unavailable'; backtest_ready: boolean; blockers: string[];
  datasets: Record<string, { label: string; provider: string | null; status: 'available' | 'missing'; rows?: number; first?: string | null; last?: string | null; instruments?: number; contracts?: number }>;
  live: { option_chain_api: string; dhan_broker_credentials_present: boolean | null; stored_quote_freshness_seconds: number | null; live_ready: boolean; note: string };
  calendar: { nse_calendar_years_configured: number[]; note: string }; disclaimer: string;
};
export type OptionDetail = {
  ok?: boolean; symbol?: string | null; strike?: string | null; expiry?: string | null; option_type?: string; delta?: number | null; delta_source?: string | null; bid?: string | null; ask?: string | null;
  spread_pct?: number | null; lot_size?: number | null; why?: string; no_trade_reasons?: string[]; quote_time?: string | null; entry_reference_ask?: string; premium_stop?: string; premium_target?: string;
  max_hold_minutes?: number; reference_label?: string; candidates?: { strike_choice: string; symbol?: string; rejected: string[]; delta?: number; spread_pct?: number }[]; mode?: string;
};
export type IndicatorParam = { default: number | string; min: number | null; max: number | null; kind: 'int' | 'float' | 'choice' | 'text'; choices: string[] };
export type IndicatorInfo = {
  id: string; name: string; category: string; outputs: string[]; formula: string; notes: string; uses_source: boolean;
  params: Record<string, IndicatorParam>; minimum_bars_default: number;
};
export type CostProfile = { id: string; label: string; market: string; effective_from: string; assumed: boolean; source_note: string; params: Record<string, string> };
export type Meta = {
  schema_version: string; engine_version: string; indicator_library_version: string; timeframes: string[]; markets: Market[]; price_fields: string[];
  compare_ops: CompareOp[]; indicators: IndicatorInfo[]; cost_profiles: CostProfile[];
  pine: { supported: string[]; partial: string[]; unsupported: string[] };
  python: { template: string; allowed_imports: string[]; timeout_seconds: number; memory_mb: number; isolation: string };
  backtest_defaults: Record<string, unknown>; disclaimer: string;
};

export type StrategyRow = {
  id: string; name: string; description: string; status: 'suspended' | 'active' | 'retired'; current_version: number; instrument_id: string;
  kind: 'intraday' | 'legacy_daily'; source_type: string; market: Market; decision_timeframe: string; created_at: string; updated_at: string;
  last_backtest: { id: string; at: string; version: number; trades: number | null; net_profit: string | null; return_pct: number | null } | null;
  params: Record<string, unknown> | null;
  origin?: 'starter' | 'customized' | null; origin_label?: string | null; starter_key?: string | null;
  derived_from?: { strategy_id: string; version: number; starter_key?: string | null; name: string } | null;
  indicator_summary?: string[]; tested?: boolean;
  strategy_type?: 'options_buying' | 'intraday' | 'legacy_daily';
  options_summary?: { market: string; type: string; directions: string[]; base_data: string; trend_timeframe: string; entry_timeframe: string; max_duration_minutes: number; premium_stop_pct: number; target_r: number; price_mode: string };
  data_coverage?: { mode: string; ready: boolean; blockers: string[]; datasets: Record<string, string> };
  versions?: VersionRow[];
  tunable_parameters?: { key: string; value: number; kind: string; min: number | null; max: number | null }[];
};
export type VersionRow = {
  version: number; created_at: string; source_type: string; definition: Definition | null; params: Record<string, unknown>;
  definition_hash: string | null; locked: boolean; has_source: boolean; source_filename: string | null;
};
export type InstrumentRow = { id: string; symbol: string; name: string; market: Market; currency: string; bars_1m: number; first_bar: string | null; last_bar: string | null };

export type Issue = { level: 'error' | 'warning'; message: string; where?: string };
export type ValidateResult = {
  valid: boolean; issues: Issue[]; definition_hash?: string;
  requirements: { timeframes: string[]; indicators: IndicatorDecl[]; warmup_base_minutes: number; condition_nodes: number } | null;
};

export type SignalRow = {
  id: string; strategy_id: string; strategy_name: string | null; strategy_version: number; instrument_id: string; symbol: string | null; market: Market;
  direction: 'long' | 'short'; status: string; suppression_reason: string | null; timeframe: string;
  signal_time: string; reference_price: string; currency: string | null; indicators: Record<string, Record<string, number | null>>;
  conditions_met: ConditionDetail[]; conditions_unmet: ConditionDetail[]; option?: OptionDetail | null; rationale: string; data_freshness: Record<string, unknown>; expires_at: string; created_at: string;
};
export type ConditionDetail = { label: string; satisfied: boolean; depth: number; kind: string; left?: number | null; right?: number | null };

export type Trade = {
  instrument_id: string; symbol: string; side: 'long' | 'short'; qty: string; signal_time: string; entry_time: string; exit_time: string; entry_raw: string; entry_fill: string;
  exit_raw: string; exit_fill: string; exit_reason: string; stop_initial: string | null; target: string | null; gross_pnl: string; slippage_cost: string; charges: string;
  cost_breakdown?: CostBreakdown; net_pnl: string; r_multiple: string | null; holding_minutes: number; ambiguous_stop_and_target_same_bar: boolean; explain: ConditionDetail[] | null; option?: TradeOption;
};
export type CostBreakdown = { schedule_id: string; buy_turnover: string; sell_turnover: string; turnover: string; brokerage: string; stt: string; exchange_txn: string; sebi: string; ipft: string; stamp_duty: string; gst: string; dp_charges: string; other: string; total: string; pct_of_turnover: string; orders: { order_id: string; turnover: string; brokerage: string }[] };
export type TradeOption = {
  option_type: string; underlying_direction: string; strike: string; expiry: string; lot_size: number; lots: number; delta: number | null; delta_source: string | null; entry_bid: string | null; entry_ask: string | null;
  entry_spread_pct: number | null; exit_bid: string | null; exit_ask: string | null; exit_spread_pct: number | null; price_source: 'quote' | 'ohlc_indicative'; selection_why: string; contract_id: string;
  master_version: string; invalidation_level: number | null; mae_pct: number; mfe_pct: number; gap_through_stop: boolean; exit_unresolved: boolean; config: string; reference: string;
};
export type OptionsReport = {
  ce_trades: number; pe_trades: number; avg_entry_spread_pct: number | null; avg_exit_spread_pct: number | null; avg_delta_at_entry: number | null; avg_mae_pct: number | null; avg_mfe_pct: number | null;
  max_holding_minutes: number | null; by_option_type: Group2; by_delta_bucket: Group2; by_expiry: Group2; by_hour_ist: Group2; by_config: Group2; by_price_source: Group2;
  greeks_sources: Record<string, number>; signals: { total: number; traded: number; no_trade_reasons: Record<string, number> }; price_basis: string;
};
export type Metrics = Record<string, number | string | null>;
export type Group2 = Record<string, { trades: number; net_pnl: string; win_rate: number | null; expectancy: string }>;
export type SingleResult = {
  metrics: Metrics; equity_curve: { t: string; equity: number; drawdown_pct: number }[]; monthly: { month: string; net_pnl: string; return_pct_of_initial: number | null }[];
  by_side: Group2; by_instrument: Group2; by_session: Group2; exit_reasons: Record<string, number>; skipped_signals: Record<string, number>;
  trade_distribution: { from: number; to: number; count: number }[] | null;
  benchmark: { label: string; per_instrument: { symbol: string; buy_and_hold_return_pct: number | null }[]; average_return_pct: number | null };
  trades: Trade[]; warnings: string[]; sample: { trades: number; reliable: boolean }; assumptions: Record<string, unknown>;
  versions: { engine: string; indicator_library: string; options_engine?: string }; options_report?: OptionsReport; dataset: { sha256: string; instruments: { symbol: string; bars: number; start: string | null; end: string | null; quality: Record<string, unknown> }[] };
  refused_instruments?: { symbol: string; reason: string }[]; data_window?: { start: string; end: string };
};
export type RunRow = {
  id: string; strategy_id: string; strategy_version: number; kind: string; status: 'queued' | 'running' | 'completed' | 'failed'; progress: number; created_at: string;
  finished_at: string | null; error: string | null; engine_version: string; indicator_version: string; definition_hash: string; config: Record<string, unknown>;
  metrics: Metrics | null; headline: Record<string, unknown>; result?: Record<string, unknown> & Partial<SingleResult>; definition_snapshot?: Definition; provenance?: unknown;
};

export type ProviderCoverage = { provider: string; start: string; end: string; last_served: string | null; minutes_per_session: number; label: string; sessions_matching: number; sessions_examined: number };
export type HealthRow = {
  instrument_id: string; symbol: string; market: string; status: string; bars?: number; last_bar_age_seconds?: number | null; missing_pct?: number | null; issues: string[];
  window_days?: number; total_bars?: number; history_start?: string | null; history_end?: string | null;
  provider_unserved?: { excluded_minutes: number; note: string } | null; provider_coverage?: ProviderCoverage | null;
};
export type BackfillMeasure = { status: string; bars: number; expected_minutes: number; missing_minutes: number; missing_pct: number | null; provider_unserved_minutes?: number };
export type BackfillInstrument = {
  instrument_id: string; symbol: string; status: 'queued' | 'running' | 'waiting_rate_limit' | 'completed' | 'unrecoverable_gaps' | 'failed';
  before: BackfillMeasure | null; after: BackfillMeasure | null; bars_fetched: number; attempts: number; reason: string | null;
  unrecoverable: { day?: string; from?: string; missing_minutes: number | null; reason: string }[]; provider_coverage: ProviderCoverage | null;
};
export type BackfillJob = {
  id: string; status: 'queued' | 'running' | 'completed' | 'failed'; error: string | null; window: { start: string; end: string }; refuse_threshold_pct: number;
  pause: { until: string; seconds: number; attempt: number; of: number; symbol: string; reason: string } | null; current: { instrument_id: string; symbol: string } | null;
  instruments_total: number; instruments_done: number; instruments: BackfillInstrument[]; already_running?: boolean;
};
export type Health = {
  counts: { total: number; active: number; suspended: number; retired: number }; recent_signals: SignalRow[]; recent_backtests: RunRow[]; data_health: HealthRow[];
};

export type PineDiagnosisGroup = { key: string; label: string; blocking: boolean; count: number; follow_on_count: number; example_line: number | null; example_message: string };
export type PineDiagnosis = {
  verdict: string | null; total_errors: number; root_cause_count: number; follow_on_count: number; groups: PineDiagnosisGroup[];
  route: { to: 'python'; reason: string; limits: string[] } | null;
};
export type PineReport = {
  filename: string; sha256: string; version: number | null; compatibility: 'full' | 'partial' | 'unsupported'; can_import: boolean; equivalence_validated: boolean; equivalence_note: string;
  items: { line: number; status: 'supported' | 'partial' | 'unsupported'; construct: string; message: string }[];
  errors: { line: number | null; message: string; cause?: string; follow_on_of?: string }[]; warnings: unknown[]; parameters: Record<string, unknown>;
  diagnosis?: PineDiagnosis;
  detected: { indicators: IndicatorDecl[]; entries: Record<string, number> }; definition: Definition | null;
};
export type PythonReport = {
  filename: string; sha256: string; screen_errors: { line: number | null; message: string }[]; can_import: boolean; definition: Definition | null; allowed_imports: string[];
  isolation: string; issues?: Issue[]; meta?: Record<string, unknown>;
};

export const RULES: { key: RuleName; label: string }[] = [
  { key: 'long_entry', label: 'Long entry' }, { key: 'short_entry', label: 'Short entry' },
  { key: 'long_exit', label: 'Long exit' }, { key: 'short_exit', label: 'Short exit' },
];

export type Readiness = Record<string, { instruments_with_1m_data: number; backtest_ready: boolean }>;

export type SignalEvent = {
  event_id: string; from_state: string | null; to_state: string; event_time: string; observed_data_time: string | null; reason_code: string; reason_detail: string | null;
  strategy_version_id: string; source: string; payload: Record<string, unknown>; backfilled: boolean;
};
export type RejectionRow = {
  id: string; scan_run_id: string | null; strategy_id: string; strategy_version: number | null; instrument_id: string; direction: string | null; reason_code: string; reason_detail: string | null;
  recorded_at: string; observed_data_time: string | null; payload: Record<string, unknown>;
};

export type JobRow = {
  id: string; job_type: 'backtest' | 'scan' | string; status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'; attempts: number; max_attempts: number;
  progress: { pct?: number; stage?: string; [k: string]: unknown }; result: Record<string, unknown> | null; error: string | null; created_at: string; started_at: string | null;
  finished_at: string | null; next_attempt_at: string; locked_by: string | null; payload: Record<string, string>;
};
export type ScheduleStrategy = {
  strategy_id: string; name: string; market: string; decision_timeframe: string; min_cadence_minutes: number; enabled: boolean; cadence_minutes: number;
  next_run_at: string | null; last_enqueued_at: string | null; last_skip_reason: string | null;
};
export type ScheduleView = {
  scans_enabled: boolean; strategies: ScheduleStrategy[]; note: string;
  request_estimate: { provider_requests_per_scan_cycle: number; basis: string; dhan_documented_limits_for_reference: string; enabled_schedules: { strategy_id: string; cadence_minutes: number; scans_per_session: number }[] };
};
export type ScanRunView = {
  scan_run_id: string; status: 'running' | 'completed' | 'failed'; trigger: 'manual' | 'schedule' | null; started_at: string | null; finished_at: string | null; strategies: number;
  pairs_scanned: number; instruments_scanned: number; new_signals: number; errors: { strategy?: string; instrument?: string; error: string }[];
  rejections: { scan_run_id: string; total: number; by_reason: Record<string, number> }; jobs: { id: string; status: string; strategy_id: string; error: string | null }[];
};
