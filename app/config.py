from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://helpdesk:helpdesk@localhost:5432/helpdesk"
    init_db: bool = True

    # LLM provider (OpenAI-compatible chat completions API).
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 60.0
    llm_rate_limit_per_minute: int = 60
    # Delay range for the static draft used when LLM_API_KEY is empty.
    llm_stub_min_delay: float = 5.0
    llm_stub_max_delay: float = 30.0

    @field_validator("database_url")
    @classmethod
    def _asyncpg_driver(cls, value: str) -> str:
        # Hosting providers hand out postgres:// URLs; SQLAlchemy needs the async driver.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix):]
        return value

    # Worker.
    # Run the worker loops inside the API process (for single-container hosting).
    embedded_worker: bool = False
    worker_concurrency: int = 20
    delivery_concurrency: int = 4
    worker_poll_seconds: float = 1.0
    job_lease_seconds: int = 300
    job_max_attempts: int = 5

    # Delivery to the helpdesk.
    delivery_timeout_seconds: float = 10.0
    delivery_lease_seconds: int = 60
    delivery_max_attempts: int = 8

    # Mock helpdesk misbehaviour, to exercise retries.
    # Base URL the worker uses to reach the mock (defaults to the request URL).
    mock_reply_base_url: str = ""
    mock_failure_rate: float = 0.2
    mock_slow_rate: float = 0.1
    mock_slow_seconds: float = 15.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
