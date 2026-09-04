"""Reflexion: one structured LLM call per failure cluster."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.llm.openai_models import resolve_openai_model
from app.learning.diagnose import insight_category_for


_ALLOWED_CATEGORIES = frozenset({
    "persona",
    "knowledge",
    "retrieval",
    "handoff",
    "greeting",
    "policy",
    "other",
})


class ReflectionDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=160)
    instruction_delta: str = Field(default="", max_length=800)
    category: str = Field(default="other")
    failure_code: str = Field(default="other")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        from app.llm.openai_strict_schema import apply_openai_strict_schema

        schema = handler(core_schema)
        return apply_openai_strict_schema(schema)


def _normalize_delta(parsed: ReflectionDelta, *, failure_code: str) -> ReflectionDelta:
    category = str(parsed.category or "").strip() or insight_category_for(failure_code)
    if category not in _ALLOWED_CATEGORIES:
        category = insight_category_for(failure_code)
    title = (parsed.title or "").strip() or f"Corrigir {failure_code}"
    return ReflectionDelta(
        title=title[:160],
        instruction_delta=(parsed.instruction_delta or "").strip()[:800],
        category=category,
        failure_code=str(parsed.failure_code or failure_code or "other")[:80],
        confidence=float(parsed.confidence or 0.5),
    )


def _evidence_payload(
    *,
    failure_code: str,
    reviews: list[dict[str, Any]],
    max_examples: int = 4,
) -> dict[str, Any]:
    examples: list[dict[str, str]] = []
    for item in reviews[:max_examples]:
        examples.append(
            {
                "customer": str(item.get("customer_text") or "")[:400],
                "agent_reply": str(item.get("agent_reply") or "")[:400],
                "outcome": str(item.get("outcome") or ""),
            }
        )
    return {
        "failure_code": failure_code,
        "evidence_count": len(reviews),
        "examples": examples,
        "constraints": [
            "Write a short operational instruction in Portuguese.",
            "Do not include prices, currency, URLs, or SKU lists.",
            "Do not claim New Store buys or appraises used watches.",
            "Do not tell the agent to skip Tray / catalog tools.",
            "Do not rewrite the full persona; one delta only.",
        ],
    }


async def reflect_cluster(
    *,
    failure_code: str,
    reviews: list[dict[str, Any]],
) -> ReflectionDelta | None:
    settings = get_settings()
    if not bool(getattr(settings, "agent_learning_reflect_enabled", True)):
        return None
    if not str(getattr(settings, "openai_api_key", "") or "").strip():
        return None
    if not reviews:
        return None
    try:
        from app.llm.openai_gateway import parse_structured_output

        model = resolve_openai_model("fast")
        parse_result = await parse_structured_output(
            model=model,
            text_format=ReflectionDelta,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você é o módulo de Reflexion do Crono (New Store Relógios). "
                        "Dado um cluster de falhas reais, escreva UM delta curto de "
                        "instrução operacional para o próximo turno. "
                        "Não invente política comercial. Não cite preços nem URLs. "
                        "A New Store não avalia nem compra relógios de particulares."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        _evidence_payload(failure_code=failure_code, reviews=reviews),
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            call_type="learning_reflect",
            timeout_seconds=20,
        )
        parsed = parse_result.parsed
        if not isinstance(parsed, ReflectionDelta):
            return None
        delta = _normalize_delta(parsed, failure_code=failure_code)
        if not delta.instruction_delta:
            return None
        return delta
    except Exception as exc:
        print("[attendance.learning.reflect_error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:160],
            "failure_code": failure_code,
        })
        return None
