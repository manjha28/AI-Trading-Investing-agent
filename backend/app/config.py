from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Trading Copilot API"
    environment: str = "development"
    frontend_origin: str = "http://localhost:5173"
    database_url: str = "postgresql+psycopg://copilot:copilot@localhost:5432/trading_copilot"
    market_data_provider: str = "mock"
    market_data_providers: str = "twelve_data,finnhub,yfinance,mock"
    twelve_data_api_key: SecretStr | None = None
    finnhub_api_key: SecretStr | None = None
    market_data_quote_freshness_seconds: int = 60
    market_data_daily_freshness_seconds: int = 172800
    market_data_intraday_freshness_seconds: int = 3600
    market_data_refresh_seconds: int = Field(default=300, gt=0)
    market_data_scheduler_enabled: bool = False

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def market_data_provider_names(self) -> tuple[str, ...]:
        return tuple(
            provider.strip().casefold()
            for provider in self.market_data_providers.split(",")
            if provider.strip()
        )


settings = Settings()
