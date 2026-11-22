from dataclasses import dataclass
import ipaddress
from datetime import date, time
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        scheme = parsed.scheme.casefold()
        hostname = parsed.hostname.casefold()
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = parsed.port
        default_port = 80 if scheme == "http" else 443
        netloc = hostname if port in {None, default_port} else f"{hostname}:{port}"
        return f"{scheme}://{netloc}"
    except ValueError:
        return None


def is_allowed_origin(
    origin: str | None,
    allowed_origins: tuple[str, ...],
) -> bool:
    if origin is None:
        return True
    normalized = normalize_origin(origin)
    if normalized is None:
        return False
    return normalized in allowed_origins


def is_trusted_proxy(peer_host: str | None, configured_proxies: tuple[str, ...]) -> bool:
    if peer_host is None:
        return False
    try:
        peer_address = ipaddress.ip_address(peer_host.split("%", maxsplit=1)[0])
    except ValueError:
        peer_address = None
    if peer_address is not None and peer_address.is_loopback:
        return True

    normalized_peer = peer_host.casefold().rstrip(".")
    for configured_proxy in configured_proxies:
        configured_proxy = configured_proxy.strip()
        try:
            if peer_address is not None and peer_address in ipaddress.ip_network(
                configured_proxy, strict=False
            ):
                return True
        except ValueError:
            if normalized_peer == configured_proxy.casefold().rstrip("."):
                return True
    return False


@dataclass
class MultibaggerConfig:
    """Thresholds of the Nexus v2 multibagger engine (Phase 6; flag ``multibagger_screen_enabled``). Anchors are (value, score) pairs for ``research_scoring._lerp``. Starting hypotheses, not tuned."""
    min_price: float = 20.0
    liquidity_floor_cr: float = 2.0                       # long-term floor (Phase 3)
    data_current_annual_months: int = 15
    data_current_quarter_months: int = 6
    min_listing_fiscal_years: int = 3
    pledge_max_pct: float = 10.0
    share_cagr_3y_max: float = 0.05
    cfo_pat_min: float = 0.5
    de_max: float = 1.0
    coverage_min: float = 3.0
    max_losses_in_3y: int = 1                             # net loss in fewer than 2 of the last 3 years
    min_data_share: float = 0.70                          # a score is computed only when this share of its weight is backed by data
    q_min_avoid: float = 40.0
    mb_q: float = 70.0
    mb_g: float = 70.0
    mb_v: float = 50.0
    mb_t: float = 50.0
    mb_cap_cr: tuple[float, float] = (500.0, 20000.0)
    qc_q: float = 70.0
    qc_g: float = 50.0
    qc_v: float = 30.0
    watch_q: float = 55.0
    watch_g: float = 60.0
    watch_t: float = 50.0
    buy_zone_x: float = 70.0
    pe_cap_at: float = 80.0
    pe_cap_v: float = 25.0
    gsec_10y_pct: float | None = None                     # set by hand; unknown (factor dropped) until set
    roce_anchors: tuple = ((10.0, 0.0), (15.0, 40.0), (20.0, 70.0), (30.0, 100.0))
    roce_min_anchors: tuple = ((8.0, 0.0), (12.0, 50.0), (18.0, 100.0))
    cash_conv_anchors: tuple = ((0.5, 0.0), (0.8, 70.0), (1.0, 100.0))
    de_anchors: tuple = ((0.0, 100.0), (0.3, 85.0), (0.5, 65.0), (1.0, 20.0))
    coverage_anchors: tuple = ((3.0, 20.0), (6.0, 60.0), (12.0, 100.0))
    promoter_anchors: tuple = ((30.0, 40.0), (50.0, 80.0), (65.0, 100.0))
    rev_cagr_anchors: tuple = ((5.0, 0.0), (12.0, 50.0), (20.0, 85.0), (30.0, 100.0))
    pat_cagr_anchors: tuple = ((5.0, 0.0), (15.0, 55.0), (25.0, 90.0), (35.0, 100.0))
    consistency_anchors: tuple = ((0.0, 0.0), (0.6, 60.0), (1.0, 100.0))
    margin_change_anchors: tuple = ((-3.0, 0.0), (0.0, 50.0), (3.0, 80.0), (6.0, 100.0))
    runway_anchors_cr: tuple = ((500.0, 40.0), (1000.0, 100.0), (15000.0, 100.0), (50000.0, 40.0), (200000.0, 0.0))
    tailwind_anchors: tuple = ((5.0, 0.0), (10.0, 50.0), (18.0, 100.0))
    peg_anchors: tuple = ((0.6, 100.0), (1.0, 80.0), (1.5, 55.0), (2.5, 20.0), (3.5, 0.0))
    pe_rel_anchors: tuple = ((0.6, 100.0), (1.0, 60.0), (1.5, 20.0), (2.0, 0.0))
    pe_to_sector_anchors: tuple = ((0.6, 90.0), (1.0, 55.0), (1.5, 20.0))
    ey_spread_anchors: tuple = ((-5.0, 0.0), (-2.0, 50.0), (0.0, 80.0), (2.0, 100.0))
    roa_anchors: tuple = ((1.0, 30.0), (1.5, 70.0), (2.0, 100.0))
    roe_anchors: tuple = ((12.0, 30.0), (15.0, 60.0), (18.0, 100.0))


@dataclass
class EmergingConfig:
    """Stage 7b: early-stage multibaggers with about a year of record. Starting hypotheses, not tuned. The never-relaxed rules stay in the main engine."""
    min_quarters: int = 4
    preferred_quarters: int = 6
    ttm_revenue_growth: float = 0.25
    quarter_revenue_yoy: float = 0.20
    ttm_pat_growth: float = 0.30
    ttm_margin_gain_pts: float = 2.0
    ttm_roce_min: float = 15.0
    ttm_roce_gain_pts: float = 5.0
    t_min: float = 50.0
    cfo_pat_min: float = 0.3
    de_max: float = 1.5
    mcap_cr: tuple[float, float] = (300.0, 10000.0)
    pe_max: float = 60.0
    peg_max: float = 1.5
    x_min: float = 50.0
    dilution_max_per_year: float = 0.10
    size_fraction: float = 0.5                      # suggested position: half a normal long-term position
    upgrade_quarters: int = 2
    upgrade_revenue_yoy: float = 0.20
    downgrade_revenue_yoy: float = 0.15
    downgrade_margin_drop_pts: float = 2.0
    margin_steady_pts: float = 1.0


@dataclass
class NewListingConfig:
    """Stage 7c: listings younger than 12 months with fewer than 4 quarterly results. Starting hypotheses, not tuned."""
    recent_days: int = 365
    min_quarters_to_graduate: int = 4
    min_ipo_base_sessions: int = 15
    max_base_depth: float = 0.35
    breakout_volume_mult: float = 1.5
    lockin_buffer_sessions: int = 5
    anchor_lockin_days: tuple[int, ...] = (30, 90)
    pre_ipo_lockin_days: int = 180                  # estimate only when the real date is not stored; labelled approximate
    weak_demand_sessions: int = 20
    swing_from_sessions: int = 15
    positional_from_sessions: int = 40
    size_fraction: float = 0.25                     # at most a quarter of a normal long-term position


class Settings(BaseSettings):
    app_name: str = "Trading Copilot API"
    environment: str = "development"
    frontend_origin: str = "http://localhost:5173"
    allowed_origins_setting: str = Field(
        default="", validation_alias="ALLOWED_ORIGINS"
    )
    trusted_proxy_hosts_setting: str = Field(
        default="127.0.0.1,::1", validation_alias="TRUSTED_PROXY_HOSTS"
    )
    database_url: str = "postgresql+psycopg://copilot:copilot@postgres:5432/trading_copilot"
    db_pool_size: int = 20                                          # the default pool (5 + 10 overflow) was smaller than the 40-thread request pool: slow reports starved every other request
    db_max_overflow: int = 20
    db_pool_timeout_seconds: float = 20.0
    request_timeout_seconds: float = 120.0                          # a request that runs longer gets a 504 instead of holding the client (and its pool connection) for minutes; 0 = off
    resend_api_key: SecretStr | None = None
    resend_from_email: str = ""
    email_verification_url: str = "http://localhost:5173/"
    market_data_providers: str = "angel_one,dhan,twelve_data,finnhub,yfinance,upstox"
    twelve_data_api_key: SecretStr | None = None
    finnhub_api_key: SecretStr | None = None
    market_data_quote_freshness_seconds: int = 60
    market_data_clock_skew_tolerance_seconds: int = Field(default=30, ge=0)
    market_data_daily_freshness_seconds: int = 172800
    market_data_intraday_freshness_seconds: int = 3600
    price_usable_max_age_seconds: int = Field(default=60, gt=0)
    # A closing price more than this many completed NSE trading sessions old is
    # `stale` even when the stored last session is unchanged (e.g. sync broke).
    # Counted with NSE_NON_TRADING_DATES, so weekends and holidays do not count.
    nse_session_close_max_trading_sessions: int = Field(default=2, gt=0)
    # Verify these bounds against the current NSE circular before changing them.
    nse_no_continuous_trading_start: time = Field(default=time(15, 15), validation_alias="NSE_NO_CONTINUOUS_TRADING_START")
    nse_no_continuous_trading_end: time = Field(default=time(15, 35), validation_alias="NSE_NO_CONTINUOUS_TRADING_END")
    daily_close_price_max_age_seconds: int = Field(default=172800, gt=0)
    portfolio_value_alert_threshold: Decimal = Field(
        default=Decimal("1.00"), ge=Decimal("0"), validation_alias="PORTFOLIO_VALUE_ALERT_THRESHOLD"
    )
    indicator_lookback_bars: int = Field(default=500, gt=0)
    market_data_refresh_seconds: int = Field(default=300, gt=0)
    market_data_scheduler_enabled: bool = False
    job_worker_enabled: bool = True                                # durable job queue worker + scan scheduler (per-owner scan switch is off by default)
    scheduler_leader_election_enabled: bool = True                 # only one replica (Postgres advisory lock) runs the in-process periodic loops
    nse_shareholding_enabled: bool = False                         # Phase 6b: NSE shareholding patterns (promoter, FII, DII, public, pledge) with filing dates. Off in code; the owner's local .env turns it on
    nse_results_enabled: bool = False                              # Phase 6b: NSE filed financial results (XBRL) with filing dates. Off in code; the owner's local .env turns it on
    nse_raw_dir: str = "var/nse_raw"                               # every NSE response and XBRL document is stored here with its download date
    nse_min_interval_seconds: float = 2.2                          # polite spacing between NSE requests (at least 2 s)
    nse_nightly_request_budget: int = 600                          # NSE requests per nightly run (the backfill script has no budget)
    nse_filings_enabled: bool = False                              # Phase 6: governance/pledge/ASM-GSM adapter for NSE public filings. DISABLED; NSE's terms of use have NOT been read, so nothing may call NSE through it
    levels_v2_enabled: bool = False                               # Nexus v2 phase 1: level fixes (services/levels_v2.py). Off = v1 byte-identical
    confidence_v2_enabled: bool = False                           # Nexus v2 phase 2: factor rank + setup grade replace the confidence badge
    multibagger_screen_enabled: bool = False                      # Phase 6: the multibagger engine feeds the long-term Quality stage and the page; the nightly shadow run happens regardless
    multibagger_gsec_10y_pct: float | None = None                  # Phase 6: 10-year G-sec yield in percent, set by hand; unset = the earnings-yield spread factor is unknown (dropped from V)
    stock_verdict_enabled: bool = False                           # Phase 7: the full stock verdict (seven lenses, a verdict and an evidence score per horizon) drives the Daily report buckets and the stock report page; computed nightly in shadow mode regardless
    leader_tag_enabled: bool = False                              # Phase 6: optional 'Leader' tag on swing/positional cards (chart stage X >= 70 and RS > 0)
    pipeline_v2_enabled: bool = False                             # Nexus v2 phases 3-4: Gate -> Quality (long-term) -> Timing, shadow mode
    ml_overlay_enabled: bool = False                              # Nexus v2 phase 5: in-house ML tie-break overlay
    level_guardrails_enabled: bool = False                        # B04 Part A: deterministic level checks (targets above price, stale levels, stop width, overhead resistance); off until the owner reviews the diff
    valuation_refresh_enabled: bool = True                        # keeps valuation inputs fresh (Dhan peers + Yahoo) without manual scripts
    valuation_refresh_max_age_hours: float = 24
    valuation_refresh_max_per_cycle: int = 300
    valuation_refresh_interval_seconds: int = 1800
    research_daily_enabled: bool = True                           # nightly backend research for each user's watchlist + held stocks
    research_daily_max_per_cycle: int = 120
    research_radar_recheck_hours: float = 3.0                     # stocks on the radar (buy signal, no entry range yet) are re-researched this often
    research_radar_max_per_pass: int = 30
    research_daily_pause_seconds: float = 2.0
    market_data_dhan_only: bool = False                           # True: price history/quotes use Dhan only, never Angel One / Yahoo fallbacks
    news_ingest_enabled: bool = True                              # scheduled news + filings ingestion (rotating per-stock coverage)
    news_ingest_interval_seconds: int = 1800
    corporate_action_sync_enabled: bool = True                     # periodically ingest Yahoo dividends/splits for held and watched instruments
    corporate_action_sync_interval_seconds: int = 7200   # an ex-date is picked up within ~2 hours, no manual step
    universe_close_refresh_enabled: bool = True                    # keep Market watch daily closes current (Dhan history, bounded batches)
    universe_close_refresh_interval_seconds: int = Field(default=1200, ge=60, le=86400)
    derivatives_scheduler_enabled: bool = False                    # daily instrument-master refresh
    derivatives_master_refresh_hour_ist: int = Field(default=8, ge=0, le=23)
    derivatives_chain_poll_enabled: bool = False                   # live option-chain snapshot polling (rate-limited)
    derivatives_chain_poll_interval_seconds: int = Field(default=300, ge=60)
    derivatives_chain_poll_max_underlyings: int = Field(default=10, ge=1, le=100)
    market_stream_enabled: bool = False
    kafka_bootstrap_servers: str = "localhost:9092"
    valkey_url: str = "redis://localhost:6379/0"
    market_stream_poll_lookback_days: int = Field(default=5, ge=1, le=30)
    watchlist_price_update_interval_ms: int = Field(default=100, ge=16)
    watchlist_aggregate_update_interval_ms: int = Field(default=250, ge=16)
    watchlist_feed_stale_after_seconds: int = Field(default=30, ge=1)
    # Optional AI narration/analysis. Off by default; everything else works with it disabled.
    ai_enabled: bool = False
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: int = Field(default=90, ge=5, le=300)
    # Authorised fundamentals feed (e.g. a licensed vendor). OFF by default; Screener.in has no authorised automated access.
    fundamentals_feed_enabled: bool = False
    fundamentals_feed_base_url: str | None = None
    fundamentals_feed_api_key: SecretStr | None = None
    fundamentals_feed_source_name: str = Field(default="licensed_feed", pattern=r"^[a-z0-9_]{3,24}$")
    fundamentals_feed_min_interval_seconds: float = Field(default=2.0, ge=0.5, le=60)
    fundamentals_feed_batch_size: int = Field(default=5, ge=1, le=50)
    fundamentals_feed_cycle_minutes: int = Field(default=30, ge=5, le=1440)
    fundamentals_feed_refresh_hours: int = Field(default=168, ge=1, le=24 * 60)
    delta_research_enabled: bool = False
    delta_research_db_path: str | None = None
    delta_research_refresh_seconds: int = Field(default=60, ge=30, le=3600)
    # Execution groundwork (no order transmission exists). "live" is never honoured: see app/execution/mode.py.
    execution_mode: Literal["research", "monitor", "live"] = "research"
    # Dhan live order execution: the owner-approved exception in AGENTS.md. OFF unless this is set deliberately on the deployment; arming
    # (per user) and execute_live (per forward test) are separate, additional requirements.
    dhan_live_execution_enabled: bool = False
    forward_test_enabled: bool = True
    master_bar_refresh_enabled: bool = True
    master_bar_refresh_budget_seconds: int = Field(default=50, ge=5, le=300)
    forward_test_interval_seconds: int = Field(default=60, ge=20, le=600)
    execution_max_order_notional_usd: Decimal = Decimal("1000")
    execution_max_total_exposure_usd: Decimal = Decimal("5000")
    execution_price_max_age_seconds: int = Field(default=120, ge=5, le=3600)
    broker_encryption_key: SecretStr | None = None
    two_factor_encryption_key: SecretStr | None = None
    broker_zerodha_enabled: bool = False
    zerodha_api_key: SecretStr | None = None
    zerodha_api_secret: SecretStr | None = None
    zerodha_redirect_url: str | None = None
    broker_upstox_enabled: bool = False
    upstox_client_id: SecretStr | None = None
    upstox_client_secret: SecretStr | None = None
    upstox_redirect_uri: str | None = None
    broker_dhan_enabled: bool = False
    dhan_api_key: SecretStr | None = None
    dhan_api_secret: SecretStr | None = None
    dhan_redirect_uri: str | None = None
    angel_one_api_key: SecretStr | None = None
    angel_one_client_public_ip: str = ""
    angel_one_client_local_ip: str = "127.0.0.1"
    angel_one_client_mac_address: str = "00:00:00:00:00:00"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def levels_v2_active(self) -> bool:
        """Nexus v2 level rules are active when their own flag OR the v2 pipeline (whose Timing stage is built on them) is on."""
        return bool(self.levels_v2_enabled or self.pipeline_v2_enabled)

    @property
    def confidence_v2_active(self) -> bool:
        """Factor rank + setup grade are active when their own flag OR the v2 pipeline (whose Rank stage is built on them) is on."""
        return bool(self.confidence_v2_enabled or self.pipeline_v2_enabled)

    @property
    def market_data_provider_names(self) -> tuple[str, ...]:
        return tuple(
            provider.strip().casefold()
            for provider in self.market_data_providers.split(",")
            if provider.strip()
        )

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        origins = [self.frontend_origin]
        origins.extend(self.allowed_origins_setting.split(","))
        return tuple(dict.fromkeys(
            normalized
            for value in origins
            if (normalized := normalize_origin(value)) is not None
        ))

    @property
    def trusted_proxy_hosts(self) -> tuple[str, ...]:
        return tuple(
            host.strip()
            for host in self.trusted_proxy_hosts_setting.split(",")
            if host.strip()
        )


# NSE/BSE equity session calendar, used only to decide whether a venue session is
# open for quote freshness. Dates as supplied in the P0-B milestone brief for 2026;
# not authoritative. Verify against the NSE trading holiday circular before relying on it.
NSE_CALENDAR_YEARS: frozenset[int] = frozenset({2026})
NSE_EQUITY_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2026, 1, 15),  # Municipal Corporation Election (Maharashtra); confirmed via the NSE trading-holiday API (CM segment) on 2026-11-11
        date(2026, 1, 26),
        date(2026, 3, 3),
        date(2026, 3, 26),
        date(2026, 3, 31),
        date(2026, 4, 3),
        date(2026, 4, 14),
        date(2026, 5, 1),
        date(2026, 5, 28),
        date(2026, 6, 26),
        date(2026, 9, 14),
        date(2026, 10, 2),
        date(2026, 10, 20),
        date(2026, 11, 10),
        date(2026, 11, 24),
        date(2026, 12, 25),
    }
)
# Public sources disagreed on these 2026 dates. 2026-01-15 was confirmed as a trading
# holiday by the NSE API on 2026-11-11 and moved to NSE_EQUITY_HOLIDAYS; 2026-03-21 is a
# Saturday (non-trading regardless) and stays here pending a circular.
NSE_UNVERIFIED_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2026, 3, 21),
    }
)
# Every date that is not a regular NSE trading day, for callers that only need
# "is this a trading day" (the single NSE holiday calendar; do not duplicate it).
NSE_NON_TRADING_DATES: frozenset[date] = NSE_EQUITY_HOLIDAYS | NSE_UNVERIFIED_HOLIDAYS
# Muhurat special session (Sunday 2026-11-08). Its hours are not configured, so
# the session state for this date is unknown.
NSE_SPECIAL_SESSION_DATES: frozenset[date] = frozenset({date(2026, 11, 8)})
# Continuous NSE trading ends at 15:30 IST. The post-close session does not move
# the official regular-session close used for valuation and freshness.
NSE_FRESHNESS_SESSION_START: time = time(9, 15)
NSE_FRESHNESS_SESSION_END: time = time(15, 30)


settings = Settings()
