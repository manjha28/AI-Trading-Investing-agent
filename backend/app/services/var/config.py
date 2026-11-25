"""Versioned, validated VAR strategy configuration (every parameter is editable; none is a proven market constant)."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseCfg(_M):
    nifty_futures: bool = True
    banknifty_futures: bool = True
    stock_futures: bool = True
    max_stock_contracts: int = Field(20, ge=0, le=200)
    min_median_daily_turnover_inr: int = Field(1_000_000_000, ge=0)
    median_spread_max_ticks: float = 2.0
    p90_spread_max_ticks: float = 3.0
    liquidity_lookback_sessions: int = 20


class StructureCfg(_M):
    value_area_pct: float = Field(70.0, gt=0, le=100)
    composite_sessions: int = Field(5, ge=1, le=60)
    tpo_bracket_minutes: int = Field(30, ge=5, le=120)
    initial_balance_minutes: int = Field(60, ge=15, le=180)
    atr_period: int = Field(14, ge=2, le=100)
    node_smoothing_bins: int = Field(3, ge=1, le=15)
    hvn_percentile: float = Field(80.0, gt=50, lt=100)
    node_min_prominence: float = Field(0.10, ge=0, le=1)
    lvn_max_ratio: float = Field(0.50, gt=0, lt=1)


class RegimeCfg(_M):
    slope_threshold: float = Field(0.05, gt=0)
    slope_lookback_bars: int = 3
    closes_window: int = 6
    closes_required: int = 4
    balanced_poc_max_atr: float = 0.10


class BreakoutCfg(_M):
    acceptance_bars: int = Field(2, ge=1, le=6)
    minimum_outside_volume_pct: float = Field(25.0, ge=0, le=100)
    delta_ratio_minimum: float = Field(0.15, ge=0, le=1)
    relative_volume_minimum: float = Field(1.2, ge=0)
    rvol_sessions: int = Field(20, ge=1)                  # window: median over up to this many prior sessions
    rvol_min_sessions: int | None = Field(None, ge=1)     # minimum prior sessions required; None = rvol_sessions (strict). A diagnostic sweep lowers it.
    maximum_retest_bars: int = Field(3, ge=1, le=10)
    retest_atr_distance: float = Field(0.10, ge=0)
    stop_buffer_atr: float = Field(0.10, ge=0)
    minimum_rr: float = Field(1.5, ge=1.0)
    max_stop_atr: float = Field(3.0, gt=0)


class ReversalCfg(_M):
    minimum_sweep_atr: float = Field(0.10, ge=0)
    maximum_sweep_atr: float = Field(0.75, gt=0)
    maximum_reentry_bars: int = Field(3, ge=1, le=10)
    rejection_delta_ratio: float = Field(0.10, ge=0, le=1)
    minimum_rr: float = Field(1.5, ge=1.0)
    relative_volume_minimum: float = Field(1.0, ge=0)
    balanced_regime_only: bool = True
    absorption_percentile: float = Field(80.0, gt=50, lt=100)
    absorption_max_progress_atr: float = Field(0.10, ge=0)
    absorption_window_bars: int = Field(3, ge=1)
    absorption_history_bars: int = Field(60, ge=10)
    stop_buffer_atr: float = Field(0.10, ge=0)
    max_stop_atr: float = Field(3.0, gt=0)

    @model_validator(mode="after")
    def _sweep(self) -> "ReversalCfg":
        if self.maximum_sweep_atr <= self.minimum_sweep_atr:
            raise ValueError("maximum_sweep_atr must exceed minimum_sweep_atr")
        return self


class DeltaCfg(_M):
    minimum_classified_volume_coverage: float = Field(80.0, ge=0, le=100)
    quote_max_age_ms: int = Field(1000, ge=0)
    tick_rule_enabled: bool = True
    pivot_left: int = Field(2, ge=1)
    pivot_right: int = Field(2, ge=1)
    min_swing_separation_bars: int = Field(3, ge=1)
    min_price_excursion_atr: float = Field(0.05, ge=0)
    min_cvd_difference: float = Field(0.0, ge=0)


class RiskCfg(_M):
    risk_per_trade_pct: float = Field(0.25, gt=0, le=5)
    max_daily_loss_pct: float = Field(0.50, gt=0, le=10)
    max_entries_per_session: int = Field(3, ge=1)
    max_consecutive_losses: int = Field(2, ge=1)
    max_simultaneous_positions: int = Field(2, ge=1)
    max_aggregate_open_risk_pct: float = Field(0.50, gt=0, le=10)
    one_index_position_at_a_time: bool = True
    cooldown_minutes_after_stop: int = Field(15, ge=0)
    min_net_reward_to_cost: float = Field(1.0, ge=0)   # k: require (G - C) / C >= k, G = gross target reward Rs, C = est. round-trip charges Rs  <=>  G >= (1+k)*C


class ExitCfg(_M):
    partial_booking_pct: float = Field(50.0, ge=0, le=100)
    partial_target_r: float = Field(1.0, gt=0)
    baseline_max_target_r: float = Field(2.5, gt=1)
    trailing_activation_r: float = Field(1.5, gt=0)
    time_stop_bars: int = Field(6, ge=1)
    minimum_mfe_for_time_stop_r: float = Field(0.5, ge=0)
    exit_model: Literal["fixed_partial", "structural_trail"] = "fixed_partial"


class SessionCfg(_M):
    earliest_entry: str = "09:30"
    no_entry_minutes_before_close: int = 45
    square_off_minutes_before_close: int = 10


class ExecutionCfg(_M):
    entry_slippage_ticks: int = Field(1, ge=0)
    exit_slippage_ticks: int = Field(1, ge=0)
    stop_slippage_ticks: int = Field(2, ge=0)
    entry_variant: Literal["5m", "1m_confirmed"] = "5m"


class ChargeCfg(_M):
    """Which fee schedule prices the run. The rates themselves live ONLY in app.services.indian_charges."""
    broker_profile: str = "dhan"
    exchange: str = "NSE"
    fee_schedule_version: str | None = None      # None = the schedule version current at run time (recorded on every result)


class VarConfig(_M):
    strategy: Literal["VAR"] = "VAR"
    version: str = "1.0.0"
    variant: Literal["A", "B", "C", "D"] = "A"   # A: VP+VWAP (the only variant stored candles can support); B: +delta/CVD; C: +TPO; D: +absorption/divergence
    engines: tuple[Literal["breakout", "reversal"], ...] = ("breakout", "reversal")
    universe: UniverseCfg = UniverseCfg()
    market_structure: StructureCfg = StructureCfg()
    regime: RegimeCfg = RegimeCfg()
    breakout: BreakoutCfg = BreakoutCfg()
    reversal: ReversalCfg = ReversalCfg()
    delta: DeltaCfg = DeltaCfg()
    risk: RiskCfg = RiskCfg()
    exits: ExitCfg = ExitCfg()
    session: SessionCfg = SessionCfg()
    execution: ExecutionCfg = ExecutionCfg()
    charges: ChargeCfg = ChargeCfg()

    @model_validator(mode="after")
    def _cross(self) -> "VarConfig":
        if self.risk.max_daily_loss_pct < self.risk.risk_per_trade_pct:
            raise ValueError("max_daily_loss_pct must be at least risk_per_trade_pct")
        if self.risk.max_aggregate_open_risk_pct < self.risk.risk_per_trade_pct:
            raise ValueError("max_aggregate_open_risk_pct must be at least risk_per_trade_pct")
        if self.exits.partial_target_r >= self.exits.baseline_max_target_r:
            raise ValueError("partial_target_r must be below baseline_max_target_r")
        if self.exits.trailing_activation_r < self.exits.partial_target_r:
            raise ValueError("trailing_activation_r must not precede the partial target")
        return self

    @property
    def uses_orderflow(self) -> bool:
        return self.variant in ("B", "C", "D")

    @property
    def uses_tpo(self) -> bool:
        return self.variant in ("C", "D")

    @property
    def uses_absorption(self) -> bool:
        return self.variant == "D"
