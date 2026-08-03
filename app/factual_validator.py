from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .agent_contracts import AgentDecision
from .models import AgentResult


_URL_RE = re.compile(r"https?://[^\s<>()]+", flags=re.IGNORECASE)
_MONEY_RE = re.compile(
    r"R\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})|[0-9]+(?:[.,][0-9]{1,2})?)",
    flags=re.IGNORECASE,
)
_ORDER_RE = re.compile(
    r"\bpedido(?:\s+n[ºo°.]*)?\s*#?\s*"
    r"((?=[A-Za-z0-9._/-]*\d)[A-Za-z0-9][A-Za-z0-9._/-]{1,})",
    flags=re.IGNORECASE,
)
_URL_KEYS = ("url", "link", "checkout")
_ORDER_KEYS = ("order_id", "order_code", "pedido_id", "pedido_codigo")
_MONEY_KEYS = (
    "price",
    "total",
    "subtotal",
    "amount",
    "value",
    "installment",
)


class FactPack(BaseModel):
    trusted_urls: set[str] = Field(default_factory=set)
    order_ids: set[str] = Field(default_factory=set)
    monetary_values: set[Decimal] = Field(default_factory=set)
    source_payload: dict[str, Any] = Field(default_factory=dict, exclude=True)


class FactualViolation(BaseModel):
    kind: Literal["url", "order_id", "money"]
    claim: str
    reason: str


class FactualValidationReport(BaseModel):
    valid: bool = True
    mode: Literal["off", "shadow", "enforce"] = "shadow"
    checked_claims: int = 0
    violations: list[FactualViolation] = Field(default_factory=list)
    fallback_applied: bool = False


def _clean_url(value: str) -> str:
    return value.rstrip(".,;:!?)]}\"'")


def _money_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace("R$", "").strip()
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _collect_facts(
    value: Any,
    *,
    key: str = "",
    pack: FactPack,
) -> None:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            _collect_facts(
                child_value,
                key=str(child_key).lower(),
                pack=pack,
            )
        return
    if isinstance(value, (list, tuple, set)):
        for child in value:
            _collect_facts(child, key=key, pack=pack)
        return

    text = str(value or "").strip()
    if not text:
        return
    if any(token in key for token in _URL_KEYS):
        for match in _URL_RE.findall(text):
            pack.trusted_urls.add(_clean_url(match))
    if key in _ORDER_KEYS:
        pack.order_ids.add(text.casefold())
    if any(token in key for token in _MONEY_KEYS):
        amount = _money_decimal(value)
        if amount is not None:
            pack.monetary_values.add(amount)


def build_fact_pack(result: AgentResult) -> FactPack:
    source_payload = {
        "commercial_data": result.commercial_data or {},
        "verified_facts": (result.response_metadata or {}).get(
            "verified_facts",
            {},
        ),
        "outbound_image_url": (result.response_metadata or {}).get(
            "outbound_image_url"
        ),
    }
    pack = FactPack(source_payload=source_payload)
    _collect_facts(source_payload, pack=pack)
    return pack


def _trusted_domain(url: str, trusted_domains: set[str]) -> bool:
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in trusted_domains
    )


def validate_factual_response(
    result: AgentResult,
    *,
    decision: AgentDecision,
    mode: Literal["off", "shadow", "enforce"] = "shadow",
    trusted_domains: set[str] | None = None,
) -> FactualValidationReport:
    report = FactualValidationReport(mode=mode)
    if mode == "off":
        return report

    pack = build_fact_pack(result)
    domains = {
        domain.lower().strip()
        for domain in (
            trusted_domains
            or {"sorteionewstore.com.br", "newstoresorteios.com.br"}
        )
        if domain.strip()
    }
    text = result.reply_text or ""

    for raw_url in _URL_RE.findall(text):
        url = _clean_url(raw_url)
        report.checked_claims += 1
        if url not in pack.trusted_urls and not _trusted_domain(url, domains):
            report.violations.append(
                FactualViolation(
                    kind="url",
                    claim=url,
                    reason="url_not_present_in_verified_facts",
                )
            )

    order_claims = _ORDER_RE.findall(text)
    if (
        order_claims
        and "transactional_facts" in decision.risk.required_validations
    ):
        for order_id in order_claims:
            report.checked_claims += 1
            if order_id.casefold() not in pack.order_ids:
                report.violations.append(
                    FactualViolation(
                        kind="order_id",
                        claim=order_id,
                        reason="order_id_not_present_in_verified_facts",
                    )
                )

    validate_money = bool(
        {"catalog_facts", "transactional_facts"}.intersection(
            decision.risk.required_validations
        )
    )
    if validate_money and decision.domain == "commerce":
        for amount_text in _MONEY_RE.findall(text):
            amount = _money_decimal(amount_text)
            if amount is None:
                continue
            report.checked_claims += 1
            if amount not in pack.monetary_values:
                report.violations.append(
                    FactualViolation(
                        kind="money",
                        claim=str(amount),
                        reason="money_not_present_in_verified_facts",
                    )
                )

    report.valid = not report.violations
    return report


def apply_factual_validation(
    result: AgentResult,
    *,
    decision: AgentDecision,
    mode: Literal["off", "shadow", "enforce"] = "shadow",
    trusted_domains: set[str] | None = None,
) -> AgentResult:
    report = validate_factual_response(
        result,
        decision=decision,
        mode=mode,
        trusted_domains=trusted_domains,
    )
    if (
        mode == "enforce"
        and not report.valid
        and report.violations
    ):
        fallback = str(
            (result.response_metadata or {}).get("factual_fallback_text")
            or ""
        ).strip()
        result.reply_text = fallback or (
            "Não consegui validar com segurança todos os dados desta resposta. "
            "Vou consultar novamente antes de confirmar."
        )
        result.reply_modality = "text"
        result.reply_audio_bytes = None
        result.reply_audio_mime_type = None
        result.reply_audio_url = None
        result.safety_reason = "factual_validation_failed"
        report.fallback_applied = True

    result.response_metadata["factual_validation"] = report.model_dump(
        mode="json"
    )
    return result
