from pydantic import BaseModel, Field
from typing import Any


class IncomingMessage(BaseModel):
    provider: str = "brevo"
    event_type: str | None = None
    message_id: str | None = None
    conversation_id: str | None = None
    sender_phone: str | None = None
    sender_name: str | None = None
    text: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    reply_text: str
    intent: str = "general_support"
    confidence: float | None = None
    handoff_required: bool = False
    safety_reason: str | None = None


class BrevoSendResult(BaseModel):
    ok: bool
    dry_run: bool = True
    status_code: int | None = None
    provider_response: dict[str, Any] | None = None
    error: str | None = None
