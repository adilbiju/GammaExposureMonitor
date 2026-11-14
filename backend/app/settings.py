from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(Path(__file__).parents[2] / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_mode: str = "demo"
    mcp_server_url: str = "https://agent.robinhood.com/mcp/trading"
    mcp_redirect_uri: str = "http://127.0.0.1:8000/api/mcp/callback"
    mcp_token_store: str = ".data/robinhood-oauth.json"
    mcp_bearer_token: str = ""
    risk_free_rate: float = 0.045
    default_auto_refresh_seconds: int = 60

    @property
    def token_store_path(self) -> Path:
        path = Path(self.mcp_token_store)
        if path.is_absolute():
            return path
        return Path(__file__).parents[2] / path


@lru_cache
def get_settings() -> Settings:
    return Settings()

