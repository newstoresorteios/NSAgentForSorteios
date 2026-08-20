"""Turn-scoped persona runtime: load once, drive flow params + prompt."""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr

from .persona_models import PersonaVersion


DEFAULT_PIX_DISCOUNT_PERCENT = 15

_PIX_DISCOUNT_RE = re.compile(
    r"(?:"
    r"(?P<a>\d{1,2})\s*%\s*(?:de\s+)?(?:desconto\s+)?"
    r"(?:no\s+|via\s+|pelo\s+|pagando\s+(?:no\s+|via\s+)?)?pix"
    r"|"
    r"pix[^\n.]{0,48}?(?P<b>\d{1,2})\s*%"
    r"|"
    r"(?:desconto|além|alem)[^\n.]{0,40}?(?P<c>\d{1,2})\s*%[^\n.]{0,24}?pix"
    r")",
    flags=re.IGNORECASE,
)

_persona_runtime_var: ContextVar["PersonaRuntimeConfig | None"] = ContextVar(
    "persona_runtime",
    default=None,
)


class PersonaRuntimeConfig(BaseModel):
    """Executable policy derived from the active persona for this turn."""

    loaded: bool = False
    enabled: bool = False
    persona_version_id: int | None = None
    tenant_id: str = "newstore"
    persona_key: str = "newstore_commercial"
    display_name: str | None = None
    chatbo_persona_id: str | None = None
    agent_display_name: str = "Crono"
    greeting_mode: Literal["persona_llm", "persona_text", "local"] = "persona_llm"
    pix_discount_percent: int = DEFAULT_PIX_DISCOUNT_PERCENT
    max_pix_discount_percent: int = DEFAULT_PIX_DISCOUNT_PERCENT
    site_price_is_final: bool = True
    require_cart_for_informational_payment: bool = False
    require_product_before_checkout: bool = True
    negotiation_beyond_pix: Literal["deny", "human_handoff"] = "human_handoff"
    policy_source: str = "defaults"
    load_error: str | None = None

    _active_persona: PersonaVersion | None = PrivateAttr(default=None)
    _chatbo_profile: dict[str, Any] | None = PrivateAttr(default=None)

    @property
    def active_persona(self) -> PersonaVersion | None:
        return self._active_persona

    @property
    def chatbo_profile(self) -> dict[str, Any] | None:
        return self._chatbo_profile

    def bind_sources(
        self,
        *,
        active_persona: PersonaVersion | None = None,
        chatbo_profile: dict[str, Any] | None = None,
    ) -> "PersonaRuntimeConfig":
        self._active_persona = active_persona
        self._chatbo_profile = chatbo_profile
        return self

    def flow_params_dict(self) -> dict[str, Any]:
        return {
            "persona_version_id": self.persona_version_id,
            "agent_display_name": self.agent_display_name,
            "greeting_mode": self.greeting_mode,
            "pix_discount_percent": self.pix_discount_percent,
            "max_pix_discount_percent": self.max_pix_discount_percent,
            "site_price_is_final": self.site_price_is_final,
            "require_cart_for_informational_payment": (
                self.require_cart_for_informational_payment
            ),
            "require_product_before_checkout": self.require_product_before_checkout,
            "negotiation_beyond_pix": self.negotiation_beyond_pix,
            "policy_source": self.policy_source,
        }

    def interpreter_policy_block(self) -> str:
        """Short structured hint for the intent interpreter (not full persona)."""
        return (
            "<persona_runtime_policy>\n"
            f"- agent_name: {self.agent_display_name}\n"
            f"- pix_discount_percent: {self.pix_discount_percent}\n"
            f"- max_pix_discount_percent: {self.max_pix_discount_percent}\n"
            f"- site_price_is_final: {self.site_price_is_final}\n"
            f"- require_cart_for_informational_payment: "
            f"{self.require_cart_for_informational_payment}\n"
            f"- require_product_before_checkout: "
            f"{self.require_product_before_checkout}\n"
            f"- negotiation_beyond_pix: {self.negotiation_beyond_pix}\n"
            "- Perguntas de desconto/PIX sem fechar compra = "
            "payment_request_kind=informational e purchase_action=null.\n"
            "- Nunca prometa desconto acima de max_pix_discount_percent.\n"
            "</persona_runtime_policy>"
        )

    def prompt_policy_block(self) -> str:
        negotiation = (
            "escalar para consultor humano"
            if self.negotiation_beyond_pix == "human_handoff"
            else "recusar e manter a política oficial"
        )
        return (
            "<persona_runtime_policy>\n"
            f"Identidade operacional: {self.agent_display_name}.\n"
            f"Desconto oficial no PIX: {self.pix_discount_percent}% "
            f"(máximo {self.max_pix_discount_percent}%).\n"
            f"Preço do site é final: {self.site_price_is_final}.\n"
            "Consulta informativa de pagamento/desconto "
            f"{'exige' if self.require_cart_for_informational_payment else 'não exige'} "
            "carrinho.\n"
            f"Negociação além do PIX oficial: {negotiation}.\n"
            "</persona_runtime_policy>"
        )


def get_persona_runtime() -> PersonaRuntimeConfig | None:
    return _persona_runtime_var.get()


def set_persona_runtime(config: PersonaRuntimeConfig | None) -> Token:
    return _persona_runtime_var.set(config)


def reset_persona_runtime(token: Token) -> None:
    _persona_runtime_var.reset(token)


def _coerce_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().casefold()
    if text in {"1", "true", "yes", "sim", "on"}:
        return True
    if text in {"0", "false", "no", "nao", "não", "off"}:
        return False
    return default


def extract_pix_discount_percent(*texts: str | None) -> int | None:
    """Best-effort parse of official PIX discount from persona prose."""
    found: list[int] = []
    for text in texts:
        body = str(text or "")
        if not body.strip():
            continue
        for match in _PIX_DISCOUNT_RE.finditer(body):
            raw = match.group("a") or match.group("b") or match.group("c")
            if raw is None:
                continue
            value = int(raw)
            if 1 <= value <= 40:
                found.append(value)
    if not found:
        return None
    # Prefer the most common official value; break ties with the first hit.
    counts: dict[int, int] = {}
    for value in found:
        counts[value] = counts.get(value, 0) + 1
    return max(counts.items(), key=lambda item: (item[1], -found.index(item[0])))[0]


def _metadata_policy(metadata: dict[str, Any] | None) -> dict[str, Any]:
    raw = metadata or {}
    for key in ("runtime_policy", "flow_params", "flowParams", "runtimePolicy"):
        value = raw.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def apply_policy_overrides(
    config: PersonaRuntimeConfig,
    policy: dict[str, Any],
    *,
    source: str,
) -> PersonaRuntimeConfig:
    if not policy:
        return config
    pix = _coerce_int(
        policy.get("pix_discount_percent", policy.get("pixDiscountPercent")),
        config.pix_discount_percent,
    )
    max_pix = _coerce_int(
        policy.get(
            "max_pix_discount_percent",
            policy.get("maxPixDiscountPercent", pix),
        ),
        pix,
    )
    pix = max(1, min(40, pix))
    max_pix = max(pix, min(40, max_pix))
    greeting_mode = str(
        policy.get("greeting_mode", policy.get("greetingMode", config.greeting_mode))
        or config.greeting_mode
    ).strip().casefold()
    if greeting_mode not in {"persona_llm", "persona_text", "local"}:
        greeting_mode = config.greeting_mode
    negotiation = str(
        policy.get(
            "negotiation_beyond_pix",
            policy.get("negotiationBeyondPix", config.negotiation_beyond_pix),
        )
        or config.negotiation_beyond_pix
    ).strip().casefold()
    if negotiation not in {"deny", "human_handoff"}:
        negotiation = config.negotiation_beyond_pix
    agent_name = str(
        policy.get(
            "agent_display_name",
            policy.get("agentDisplayName", config.agent_display_name),
        )
        or config.agent_display_name
    ).strip() or config.agent_display_name
    updated = config.model_copy(
        update={
            "pix_discount_percent": pix,
            "max_pix_discount_percent": max_pix,
            "site_price_is_final": _coerce_bool(
                policy.get(
                    "site_price_is_final",
                    policy.get("sitePriceIsFinal", config.site_price_is_final),
                ),
                config.site_price_is_final,
            ),
            "require_cart_for_informational_payment": _coerce_bool(
                policy.get(
                    "require_cart_for_informational_payment",
                    policy.get(
                        "requireCartForInformationalPayment",
                        config.require_cart_for_informational_payment,
                    ),
                ),
                config.require_cart_for_informational_payment,
            ),
            "require_product_before_checkout": _coerce_bool(
                policy.get(
                    "require_product_before_checkout",
                    policy.get(
                        "requireProductBeforeCheckout",
                        config.require_product_before_checkout,
                    ),
                ),
                config.require_product_before_checkout,
            ),
            "negotiation_beyond_pix": negotiation,
            "greeting_mode": greeting_mode,
            "agent_display_name": agent_name,
            "policy_source": source,
        }
    )
    return updated.bind_sources(
        active_persona=config.active_persona,
        chatbo_profile=config.chatbo_profile,
    )


def build_persona_runtime(
    *,
    active: PersonaVersion | None,
    chatbo_profile: dict[str, Any] | None = None,
    tenant_id: str = "newstore",
    persona_key: str = "newstore_commercial",
    enabled: bool = True,
    load_error: str | None = None,
) -> PersonaRuntimeConfig:
    config = PersonaRuntimeConfig(
        loaded=True,
        enabled=bool(enabled and active is not None),
        persona_version_id=getattr(active, "id", None) if active else None,
        tenant_id=tenant_id,
        persona_key=persona_key,
        display_name=getattr(active, "name", None) if active else None,
        load_error=load_error,
    )
    config.bind_sources(active_persona=active, chatbo_profile=chatbo_profile)

    if active is None:
        return config

    from .persona_knowledge_repository import chatbo_persona_id

    config.chatbo_persona_id = chatbo_persona_id(active.metadata)
    meta_policy = _metadata_policy(active.metadata)
    if meta_policy:
        config = apply_policy_overrides(config, meta_policy, source="metadata")
        config.bind_sources(active_persona=active, chatbo_profile=chatbo_profile)
        config.chatbo_persona_id = chatbo_persona_id(active.metadata)
        return config

    restriction_text = ""
    if chatbo_profile:
        restriction_text = "\n".join(
            str(chatbo_profile.get(key) or "")
            for key in (
                "restrictions",
                "objection_handling",
                "sales_goals",
                "introduction",
            )
        )
    parsed = extract_pix_discount_percent(active.instructions, restriction_text)
    if parsed is not None:
        config = config.model_copy(
            update={
                "pix_discount_percent": parsed,
                "max_pix_discount_percent": parsed,
                "policy_source": "instructions_parse",
            }
        )
        config.bind_sources(active_persona=active, chatbo_profile=chatbo_profile)
        config.chatbo_persona_id = chatbo_persona_id(active.metadata)
    return config


def load_persona_runtime() -> PersonaRuntimeConfig:
    """Load active persona + derive executable flow params for this turn."""
    from .config import get_settings
    from .persona_repository import (
        DEFAULT_PERSONA_KEY,
        DEFAULT_TENANT_ID,
        get_active_persona,
    )

    settings = get_settings()
    tenant_id = str(
        getattr(settings, "agent_persona_tenant_id", DEFAULT_TENANT_ID)
        or DEFAULT_TENANT_ID
    )
    persona_key = str(
        getattr(settings, "agent_persona_key", DEFAULT_PERSONA_KEY)
        or DEFAULT_PERSONA_KEY
    )
    enabled = bool(getattr(settings, "agent_db_persona_enabled", False))
    if not enabled:
        return PersonaRuntimeConfig(
            loaded=True,
            enabled=False,
            tenant_id=tenant_id,
            persona_key=persona_key,
            policy_source="db_persona_disabled",
        )

    active: PersonaVersion | None = None
    load_error: str | None = None
    try:
        active = get_active_persona(tenant_id, persona_key)
    except Exception as exc:
        load_error = f"persona_load_failed:{type(exc).__name__}"
        print("[persona.runtime.load_error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:160],
        })

    chatbo_profile: dict[str, Any] | None = None
    if active is not None:
        try:
            from .persona_knowledge_repository import (
                chatbo_persona_id,
                get_chatbo_persona_profile,
            )

            chatbo_id = chatbo_persona_id(active.metadata)
            if chatbo_id:
                chatbo_profile = get_chatbo_persona_profile(chatbo_id)
        except Exception as exc:
            print("[persona.runtime.chatbo_error]", {
                "error_type": type(exc).__name__,
                "error": str(exc)[:160],
            })

    config = build_persona_runtime(
        active=active,
        chatbo_profile=chatbo_profile,
        tenant_id=tenant_id,
        persona_key=persona_key,
        enabled=enabled,
        load_error=load_error,
    )
    print("[persona.runtime.loaded]", {
        "enabled": config.enabled,
        "persona_version_id": config.persona_version_id,
        "policy_source": config.policy_source,
        "pix_discount_percent": config.pix_discount_percent,
        "require_cart_for_informational_payment": (
            config.require_cart_for_informational_payment
        ),
        "greeting_mode": config.greeting_mode,
    })
    return config
