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
    openai_transcribe_model: str = Field(default="whisper-1", alias="OPENAI_TRANSCRIBE_MODEL")
    openai_tts_model: str = Field(default="gpt-4o-mini-tts", alias="OPENAI_TTS_MODEL")
    openai_tts_voice: str = Field(default="nova", alias="OPENAI_TTS_VOICE")

    audio_inbound_enabled: bool = Field(default=True, alias="AUDIO_INBOUND_ENABLED")
    audio_outbound_enabled: bool = Field(default=True, alias="AUDIO_OUTBOUND_ENABLED")
    audio_public_base_url: str = Field(default="", alias="AUDIO_PUBLIC_BASE_URL")

    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_service_key: str = Field(default="", alias="SUPABASE_SERVICE_KEY")
    supabase_audio_bucket: str = Field(default="agent-audio", alias="SUPABASE_AUDIO_BUCKET")

    database_url: str = Field(default="", alias="DATABASE_URL")
    auto_create_tables: bool = Field(default=True, alias="AUTO_CREATE_TABLES")

    brevo_api_key: str = Field(default="", alias="BREVO_API_KEY")
    brevo_send_url: str = Field(default="", alias="BREVO_SEND_URL")
    brevo_sender_number: str = Field(default="", alias="BREVO_SENDER_NUMBER")
    brevo_reply_mode: str = Field(default="dry_run", alias="BREVO_REPLY_MODE")
    brevo_agent_id: str = Field(default="", alias="BREVO_AGENT_ID")
    brevo_agent_email: str = Field(default="", alias="BREVO_AGENT_EMAIL")
    brevo_agent_name: str = Field(default="NewStoreAgent", alias="BREVO_AGENT_NAME")
    brevo_received_from: str = Field(default="NewStoreAgent", alias="BREVO_RECEIVED_FROM")

    max_reply_chars: int = Field(default=900, alias="MAX_REPLY_CHARS")

    @field_validator("openai_api_key", "admin_api_token", "brevo_webhook_secret", "brevo_api_key", mode="before")
    @classmethod
    def normalize_secret(cls, value: object) -> object:
        if isinstance(value, str):
            return _strip_secret(value)
        return value

    @field_validator("supabase_service_key", mode="before")
    @classmethod
    def normalize_supabase_key(cls, value: object) -> object:
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
