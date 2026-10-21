from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Trading Copilot API"
    environment: str = "development"
    frontend_origin: str = "http://localhost:5173"
    database_url: str = "postgresql+psycopg://copilot:copilot@postgres:5432/trading_copilot"
    resend_api_key: SecretStr | None = None
    resend_from_email: str = ""
    email_verification_url: str = "http://localhost:5173/"
    market_data_providers: str = "delta_exchange,binance,yfinance,upstox,mock"
    twelve_data_api_key: SecretStr | None = None
    finnhub_api_key: SecretStr | None = None
    market_data_quote_freshness_seconds: int = 60
    market_data_daily_freshness_seconds: int = 172800
    market_data_intraday_freshness_seconds: int = 3600
    market_data_refresh_seconds: int = Field(default=300, gt=0)
    market_data_scheduler_enabled: bool = False
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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def market_data_provider_names(self) -> tuple[str, ...]:
        return tuple(
            provider.strip().casefold()
            for provider in self.market_data_providers.split(",")
            if provider.strip()
        )


settings = Settings()
