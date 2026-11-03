import ipaddress
from datetime import date, time
from decimal import Decimal
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
    resend_api_key: SecretStr | None = None
    resend_from_email: str = ""
    email_verification_url: str = "http://localhost:5173/"
    market_data_providers: str = "angel_one,dhan,twelve_data,finnhub,delta_exchange,binance,yfinance,upstox"
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
    market_stream_enabled: bool = False
    kafka_bootstrap_servers: str = "localhost:9092"
    valkey_url: str = "redis://localhost:6379/0"
    market_stream_poll_lookback_days: int = Field(default=5, ge=1, le=30)
    watchlist_price_update_interval_ms: int = Field(default=100, ge=16)
    watchlist_aggregate_update_interval_ms: int = Field(default=250, ge=16)
    watchlist_feed_stale_after_seconds: int = Field(default=30, ge=1)
    delta_research_enabled: bool = False
    delta_research_db_path: str | None = None
    delta_research_refresh_seconds: int = Field(default=60, ge=30, le=3600)
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
# Public sources disagree on these 2026 dates. They are not included in
# NSE_EQUITY_HOLIDAYS; a session on these days is reported as unknown.
NSE_UNVERIFIED_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2026, 1, 15),
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
