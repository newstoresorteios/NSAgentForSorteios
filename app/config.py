from functools import lru_cache
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _strip_secret(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = Field(default="production", alias="ENVIRONMENT")
    log_level: str = Field(default="info", alias="LOG_LEVEL")
    app_name: str = Field(default="NewStoreAgent", alias="APP_NAME")
    dry_run: bool = Field(default=True, alias="DRY_RUN")

    brevo_webhook_secret: str = Field(default="", alias="BREVO_WEBHOOK_SECRET")
    admin_api_token: str = Field(default="", alias="ADMIN_API_TOKEN")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-3.5-turbo", alias="OPENAI_MODEL")
    openai_agent_name: str = Field(default="NewStoreAgent", alias="OPENAI_AGENT_NAME")

    database_url: str = Field(default="", alias="DATABASE_URL")
    auto_create_tables: bool = Field(default=True, alias="AUTO_CREATE_TABLES")

    brevo_api_key: str = Field(default="", alias="BREVO_API_KEY")
    brevo_send_url: str = Field(default="", alias="BREVO_SEND_URL")
    brevo_sender_number: str = Field(default="", alias="BREVO_SENDER_NUMBER")
    brevo_reply_mode: str = Field(default="dry_run", alias="BREVO_REPLY_MODE")

    max_reply_chars: int = Field(default=900, alias="MAX_REPLY_CHARS")

    @field_validator("openai_api_key", "admin_api_token", "brevo_webhook_secret", "brevo_api_key", mode="before")
    @classmethod
    def normalize_secret(cls, value: object) -> object:
        if isinstance(value, str):
            return _strip_secret(value)
        return value

    @field_validator("openai_model", mode="before")
    @classmethod
    def normalize_model(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
