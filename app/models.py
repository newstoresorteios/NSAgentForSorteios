from pydantic import BaseModel, Field, PrivateAttr
from typing import Any, Literal


class ProductSubject(BaseModel):
    product_type: str | None = None
    brand: str | None = None
    model: str | None = None
    reference: str | None = None
    ean: str | None = None


class ProductPreferences(BaseModel):
    budget_min: float | None = None
    budget_max: float | None = None
    color: str | None = None
    style: str | None = None
    occasion: str | None = None
    recipient: str | None = None
    attributes: list[str] = Field(default_factory=list)


class SalesInterpretation(BaseModel):
    domain: Literal[
        "commerce",
        "raffle",
        "store_general",
        "greeting",
        "out_of_scope",
    ]
    goal: Literal[
        "discover",
        "find",
        "recommend",
        "compare",
        "inspect",
        "buy",
        "after_sales",
    ] | None = None
    subject: ProductSubject = Field(default_factory=ProductSubject)
    preferences: ProductPreferences = Field(default_factory=ProductPreferences)
    references_previous_context: bool
    needs_clarification: bool
    clarification_question: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)

    _source: str = PrivateAttr(default="openai")


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
    transcription_failed: bool = False
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
    commercial_data: dict[str, Any] | None = None

    model_config = {"arbitrary_types_allowed": True}


class BrevoSendResult(BaseModel):
    ok: bool
    dry_run: bool = True
    status_code: int | None = None
    provider_response: dict[str, Any] | None = None
    error: str | None = None
