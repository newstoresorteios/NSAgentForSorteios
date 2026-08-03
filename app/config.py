from functools import lru_cache
from typing import Literal

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
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    openai_agent_name: str = Field(default="NewStoreAgent", alias="OPENAI_AGENT_NAME")
    openai_transcribe_model: str = Field(default="whisper-1", alias="OPENAI_TRANSCRIBE_MODEL")
    openai_tts_model: str = Field(default="gpt-4o-mini-tts", alias="OPENAI_TTS_MODEL")
    openai_tts_voice: str = Field(default="nova", alias="OPENAI_TTS_VOICE")
    openai_tts_format: str = Field(default="opus", alias="OPENAI_TTS_FORMAT")
    agent_runtime_enabled: bool = Field(default=True, alias="AGENT_RUNTIME_ENABLED")
    agent_llm_budget_enabled: bool = Field(
        default=False,
        alias="AGENT_LLM_BUDGET_ENABLED",
    )
    agent_max_llm_calls_per_turn: int = Field(
        default=2,
        alias="AGENT_MAX_LLM_CALLS_PER_TURN",
        ge=0,
    )
    agent_policy_mode: Literal["off", "shadow", "enforce"] = Field(
        default="shadow",
        alias="AGENT_POLICY_MODE",
    )
    agent_factual_validation_mode: Literal[
        "off",
        "shadow",
        "enforce",
    ] = Field(
        default="shadow",
        alias="AGENT_FACTUAL_VALIDATION_MODE",
    )
    agent_trusted_fact_domains: str = Field(
        default="sorteionewstore.com.br,newstoresorteios.com.br",
        alias="AGENT_TRUSTED_FACT_DOMAINS",
    )
    agent_conversation_lock_enabled: bool = Field(
        default=True,
        alias="AGENT_CONVERSATION_LOCK_ENABLED",
    )
    agent_conversation_lock_timeout_seconds: float = Field(
        default=15.0,
        alias="AGENT_CONVERSATION_LOCK_TIMEOUT_SECONDS",
        gt=0,
        le=60,
    )
    agent_quality_judge_mode: Literal["off", "shadow", "enforce"] = Field(
        default="shadow",
        alias="AGENT_QUALITY_JUDGE_MODE",
    )
    agent_quality_judge_risk_threshold: int = Field(
        default=70,
        alias="AGENT_QUALITY_JUDGE_RISK_THRESHOLD",
        ge=0,
        le=100,
    )
    # Dual-agent critique: judge draft with API catalog and retry before send.
    # Start in off; use shadow then enforce after validating latency/cost.
    agent_critique_mode: Literal["off", "shadow", "enforce"] = Field(
        default="off",
        alias="AGENT_CRITIQUE_MODE",
    )
    agent_critique_max_retries: int = Field(
        default=2,
        alias="AGENT_CRITIQUE_MAX_RETRIES",
        ge=0,
        le=10,
    )
    # Max conversation turns loaded for LLM + critique (hard capped).
    agent_history_limit: int = Field(
        default=80,
        alias="AGENT_HISTORY_LIMIT",
        ge=8,
        le=200,
    )
    agent_history_hard_cap: int = Field(
        default=80,
        alias="AGENT_HISTORY_HARD_CAP",
        ge=8,
        le=200,
    )
    agent_send_idempotency_enabled: bool = Field(
        default=True,
        alias="AGENT_SEND_IDEMPOTENCY_ENABLED",
    )

    tray_adapter_url: str = Field(default="", alias="TRAY_ADAPTER_URL")
    tray_adapter_token: str = Field(default="", alias="TRAY_ADAPTER_TOKEN")

    audio_inbound_enabled: bool = Field(default=True, alias="AUDIO_INBOUND_ENABLED")
    audio_outbound_enabled: bool = Field(default=True, alias="AUDIO_OUTBOUND_ENABLED")
    brevo_send_audio_as_attachment: bool = Field(default=True, alias="BREVO_SEND_AUDIO_AS_ATTACHMENT")
    audio_public_base_url: str = Field(default="", alias="AUDIO_PUBLIC_BASE_URL")
    checkout_cep_lookup_enabled: bool = Field(
        default=True,
        alias="CHECKOUT_CEP_LOOKUP_ENABLED",
    )
    checkout_cep_lookup_url: str = Field(
        default="https://viacep.com.br/ws",
        alias="CHECKOUT_CEP_LOOKUP_URL",
    )

    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_service_key: str = Field(default="", alias="SUPABASE_SERVICE_KEY")
    supabase_audio_bucket: str = Field(default="agent-audio", alias="SUPABASE_AUDIO_BUCKET")

    database_url: str = Field(default="", alias="DATABASE_URL")
    auto_create_tables: bool = Field(default=False, alias="AUTO_CREATE_TABLES")
    remarketing_enabled: bool = Field(default=False, alias="REMARKETING_ENABLED")
    remarketing_cron_secret: str = Field(default="", alias="CRON_SECRET")
    remarketing_touch_hours: str = Field(default="1,12,23", alias="REMARKETING_TOUCH_HOURS")
    remarketing_meta_window_hours: int = Field(default=24, alias="REMARKETING_META_WINDOW_HOURS")
    remarketing_batch_size: int = Field(default=25, alias="REMARKETING_BATCH_SIZE")

    brevo_api_key: str = Field(default="", alias="BREVO_API_KEY")
    brevo_send_url: str = Field(default="", alias="BREVO_SEND_URL")
    brevo_sender_number: str = Field(default="", alias="BREVO_SENDER_NUMBER")
    brevo_reply_mode: str = Field(default="auto", alias="BREVO_REPLY_MODE")
    brevo_agent_id: str = Field(default="", alias="BREVO_AGENT_ID")
    brevo_agent_email: str = Field(default="", alias="BREVO_AGENT_EMAIL")
    brevo_agent_name: str = Field(default="NewStoreAgent", alias="BREVO_AGENT_NAME")
    brevo_received_from: str = Field(default="NewStoreAgent", alias="BREVO_RECEIVED_FROM")
    brevo_allowed_channels: str = Field(
        default="whatsapp,instagram,facebook",
        alias="BREVO_ALLOWED_CHANNELS",
    )
    brevo_social_channels_enabled: bool = Field(
        default=True,
        alias="BREVO_SOCIAL_CHANNELS_ENABLED",
    )

    max_reply_chars: int = Field(default=900, alias="MAX_REPLY_CHARS")

    @field_validator(
        "openai_api_key", "admin_api_token", "brevo_webhook_secret", "brevo_api_key",
        "tray_adapter_token", "remarketing_cron_secret", mode="before"
    )
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


def get_allowed_channels(settings: Settings) -> set[str]:
    return {
        channel.strip().lower()
        for channel in (
            getattr(settings, "brevo_allowed_channels", "whatsapp,instagram,facebook") or ""
        ).split(",")
        if channel.strip()
    }


def get_remarketing_touch_hours(settings: Settings) -> list[int]:
    hours: set[int] = set()
    for value in (settings.remarketing_touch_hours or "").split(","):
        try:
            hour = int(value.strip())
        except ValueError:
            continue
        if 0 < hour < settings.remarketing_meta_window_hours:
            hours.add(hour)
    return sorted(hours)
