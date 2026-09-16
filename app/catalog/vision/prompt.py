"""Vision schema and commercial identification prompt."""

from __future__ import annotations

import unicodedata

from pydantic import BaseModel, Field

from app.configuration.runtime import message as operator_message
from app.catalog.retrieval.aliases import vision_commercial_model_rules

def image_identify_instructions() -> str:
    return operator_message(
        "image_identify_instructions",
        commercial_model_rules=vision_commercial_model_rules(),
    )


class ImageProductIdentification(BaseModel):
    is_watch: bool = True
    brand: str | None = None
    model: str | None = None
    reference: str | None = None
    color: str | None = None
    case_finish: str | None = None
    features: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str | None = None


def normalize_feature_label(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    folded = "".join(
        char
        for char in unicodedata.normalize("NFKD", text).lower()
        if not unicodedata.combining(char)
    )
    if "crono" in folded or "chrono" in folded:
        return "Cronógrafo"
    if "diver" in folded or "mergulho" in folded or "200m" in folded or "200 m" in folded:
        return "Mergulho"
    if "gmt" in folded:
        return "GMT"
    if "automatic" in folded or "automatico" in folded:
        return "Automático"
    if "quartz" in folded:
        return "Quartz"
    return text
