from pydantic import BaseModel, Field
from typing import Any


class IncomingMessage(BaseModel):
    provider: str = "brevo"
    event_type: str | None = None
    message_id: str | None = None
    conversation_id: str | None = None
    visitor_id: str | None = None
    sender_phone: str | None = None
    sender_name: str | None = None
    text: str = ""
    input_modality: str = "text"
    audio_url: str | None = None
    audio_mime_type: str | None = None
    audio_filename: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    reply_text: str
    intent: str = "general_support"
    confidence: float | None = None
    handoff_required: bool = False
    safety_reason: str | None = None
    reply_modality: str = "text"
    reply_audio_bytes: bytes | None = None
    reply_audio_mime_type: str | None = None
    reply_audio_url: str | None = None

    model_config = {"arbitrary_types_allowed": True}


class BrevoSendResult(BaseModel):
    ok: bool
    dry_run: bool = True
    status_code: int | None = None
    provider_response: dict[str, Any] | None = None
    error: str | None = None
