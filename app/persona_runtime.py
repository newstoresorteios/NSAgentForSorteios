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
    greeting_text: str | None = None
    closing_message: str | None = None
    tone: str | None = None
    tone_details: str | None = None
    customer_address_style: str | None = None
    greeting_mode: Literal["persona_llm", "persona_text", "local"] = "persona_text"
    pix_discount_percent: int = DEFAULT_PIX_DISCOUNT_PERCENT
    max_pix_discount_percent: int = DEFAULT_PIX_DISCOUNT_PERCENT
    site_price_is_final: bool = True
    require_cart_for_informational_payment: bool = False
    require_product_before_checkout: bool = True
    require_qualification_before_catalog: bool = True
    qualification_prompts: list[str] = Field(default_factory=list)
    max_catalog_options: int = 3
    prefer_ready_stock: bool = False
    require_official_catalog_link: bool = True
    justify_recommendations: bool = True
    recommendation_rule_texts: list[str] = Field(default_factory=list)
    objection_prompts: list[str] = Field(default_factory=list)
    sales_goal_prompts: list[str] = Field(default_factory=list)
    example_prompts: list[str] = Field(default_factory=list)
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
            "greeting_text": self.greeting_text,
            "closing_message": self.closing_message,
            "tone": self.tone,
            "greeting_mode": self.greeting_mode,
            "pix_discount_percent": self.pix_discount_percent,
            "max_pix_discount_percent": self.max_pix_discount_percent,
            "site_price_is_final": self.site_price_is_final,
            "require_cart_for_informational_payment": (
                self.require_cart_for_informational_payment
            ),
            "require_product_before_checkout": self.require_product_before_checkout,
            "require_qualification_before_catalog": (
                self.require_qualification_before_catalog
            ),
            "qualification_prompt_count": len(self.qualification_prompts),
            "max_catalog_options": self.max_catalog_options,
            "prefer_ready_stock": self.prefer_ready_stock,
            "require_official_catalog_link": self.require_official_catalog_link,
            "justify_recommendations": self.justify_recommendations,
            "negotiation_beyond_pix": self.negotiation_beyond_pix,
            "policy_source": self.policy_source,
            "chatbo_persona_id": self.chatbo_persona_id,
        }

    def sales_skills_block(self, *, max_items: int = 4) -> str:
        """Compact ChatBo sales skills for the interpreter (not full prose dump)."""
        lines = ["<persona_sales_skills>"]
        if self.qualification_prompts:
            lines.append("Qualificação (pergunte antes de listar catálogo amplo):")
            for item in self.qualification_prompts[:max_items]:
                lines.append(f"- {item}")
        if self.recommendation_rule_texts:
            lines.append("Recomendação:")
            for item in self.recommendation_rule_texts[:max_items]:
                lines.append(f"- {item}")
            lines.append(
                f"- Limite operacional de opções: {self.max_catalog_options}."
            )
            if self.prefer_ready_stock:
                lines.append("- Priorizar pronta entrega quando houver urgência.")
        if self.objection_prompts:
            lines.append("Objeções:")
            for item in self.objection_prompts[:max_items]:
                lines.append(f"- {item}")
        if self.sales_goal_prompts:
            lines.append("Objetivos:")
            for item in self.sales_goal_prompts[:max_items]:
                lines.append(f"- {item}")
        if self.example_prompts:
            lines.append("Exemplos de boa resposta:")
            for item in self.example_prompts[: min(2, max_items)]:
                lines.append(f"- {item}")
        lines.append("</persona_sales_skills>")
        if len(lines) <= 2:
            return ""
        return "\n".join(lines)

    def interpreter_policy_block(self) -> str:
        """Short structured hint for the intent interpreter (not full persona)."""
        skills = self.sales_skills_block()
        skills_section = f"\n{skills}\n" if skills else "\n"
        return (
            "<persona_runtime_policy>\n"
            f"- agent_name: {self.agent_display_name}\n"
            f"- tone: {self.tone or 'consultative'}\n"
            f"- pix_discount_percent: {self.pix_discount_percent}\n"
            f"- max_pix_discount_percent: {self.max_pix_discount_percent}\n"
            f"- site_price_is_final: {self.site_price_is_final}\n"
            f"- require_cart_for_informational_payment: "
            f"{self.require_cart_for_informational_payment}\n"
            f"- require_product_before_checkout: "
            f"{self.require_product_before_checkout}\n"
            f"- require_qualification_before_catalog: "
            f"{self.require_qualification_before_catalog}\n"
            f"- max_catalog_options: {self.max_catalog_options}\n"
            f"- prefer_ready_stock: {self.prefer_ready_stock}\n"
            f"- negotiation_beyond_pix: {self.negotiation_beyond_pix}\n"
            "- Use o perfil ChatBo completo no system prompt "
            "(saudação, objeções, recomendação, handoff).\n"
            "- Se require_qualification_before_catalog=true e o cliente só "
            "citou marca/categoria sem estilo, orçamento, ocasião ou urgência, "
            "use goal=discover, needs_clarification=true e "
            "ready_for_retrieval=false (uma pergunta curta).\n"
            "- Liberar catálogo quando houver brand+budget, brand+style, "
            "brand+urgência, type+budget+style, modelo/referência explícitos "
            "ou stop_clarification.\n"
            "- Modelo/referência explícitos liberam busca imediata.\n"
            "- Perguntas de desconto/PIX sem fechar compra = "
            "payment_request_kind=informational e purchase_action=null.\n"
            "- Nunca prometa desconto acima de max_pix_discount_percent.\n"
            "- Nunca liste mais opções do que max_catalog_options.\n"
            f"</persona_runtime_policy>{skills_section}"
        )

    def prompt_policy_block(self) -> str:
        negotiation = (
            "escalar para consultor humano"
            if self.negotiation_beyond_pix == "human_handoff"
            else "recusar e manter a política oficial"
        )
        lines = [
            "<persona_runtime_policy>",
            f"Identidade operacional: {self.agent_display_name}.",
        ]
        if self.tone:
            lines.append(f"Tom de voz: {self.tone}.")
        if self.greeting_text:
            lines.append(f"Saudação oficial: {self.greeting_text}")
        if self.customer_address_style:
            lines.append(f"Tratamento ao cliente: {self.customer_address_style}")
        if self.closing_message:
            lines.append(f"Encerramento padrão: {self.closing_message}")
        lines.extend(
            [
                f"Desconto oficial no PIX: {self.pix_discount_percent}% "
                f"(máximo {self.max_pix_discount_percent}%).",
                f"Preço do site é final: {self.site_price_is_final}.",
                "Consulta informativa de pagamento/desconto "
                f"{'exige' if self.require_cart_for_informational_payment else 'não exige'} "
                "carrinho.",
                "Antes de listar catálogo: "
                f"{'pergunte preferências da qualificação ChatBo' if self.require_qualification_before_catalog else 'pode buscar se o cliente pedir opções'}.",
                f"Máximo de peças por resposta: {self.max_catalog_options}.",
                f"Priorizar pronta entrega: {self.prefer_ready_stock}.",
                f"Negociação além do PIX oficial: {negotiation}.",
                "Siga o bloco <persona_knowledge> / persona ChatBo completo.",
                "</persona_runtime_policy>",
            ]
        )
        return "\n".join(lines)


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
            "max_catalog_options": max(
                1,
                min(
                    5,
                    _coerce_int(
                        policy.get(
                            "max_catalog_options",
                            policy.get("maxCatalogOptions", config.max_catalog_options),
                        ),
                        config.max_catalog_options,
                    ),
                ),
            ),
            "prefer_ready_stock": _coerce_bool(
                policy.get(
                    "prefer_ready_stock",
                    policy.get("preferReadyStock", config.prefer_ready_stock),
                ),
                config.prefer_ready_stock,
            ),
            "require_official_catalog_link": _coerce_bool(
                policy.get(
                    "require_official_catalog_link",
                    policy.get(
                        "requireOfficialCatalogLink",
                        config.require_official_catalog_link,
                    ),
                ),
                config.require_official_catalog_link,
            ),
            "justify_recommendations": _coerce_bool(
                policy.get(
                    "justify_recommendations",
                    policy.get(
                        "justifyRecommendations",
                        config.justify_recommendations,
                    ),
                ),
                config.justify_recommendations,
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


def _as_prompt_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                out.append(text)
        return out
    return []


_MAX_OPTIONS_RE = re.compile(
    r"(?:"
    r"mais\s+de\s+(?P<a>\d+)\s*pe[cç]as"
    r"|"
    r"(?:no\s+)?m[aá]ximo\s+(?:de\s+)?(?P<b>\d+)"
    r"|"
    r"at[eé]\s+(?P<c>\d+)\s*pe[cç]as"
    r"|"
    r"max(?:imo)?[_\s-]*(?P<d>\d+)"
    r")",
    flags=re.IGNORECASE,
)


def parse_recommendation_policy(rules: Any) -> dict[str, Any]:
    """Derive executable ranking/presentation params from ChatBo recommendation_rules."""
    texts = _as_prompt_list(rules)
    blob = "\n".join(texts).casefold()
    max_options = 3
    for text in texts:
        match = _MAX_OPTIONS_RE.search(text)
        if not match:
            continue
        raw = match.group("a") or match.group("b") or match.group("c") or match.group("d")
        if raw is None:
            continue
        value = int(raw)
        if 1 <= value <= 5:
            max_options = value
            break
    return {
        "recommendation_rule_texts": texts,
        "max_catalog_options": max_options,
        "prefer_ready_stock": any(
            token in blob
            for token in ("pronta entrega", "pronta-entrega", "urgência", "urgencia")
        ),
        "require_official_catalog_link": any(
            token in blob for token in ("link oficial", "catálogo integrado", "catalogo integrado")
        )
        or bool(texts),
        "justify_recommendations": any(
            token in blob for token in ("justificar", "razão concreta", "razao concreta")
        )
        or bool(texts),
    }


def _enrich_from_chatbo_profile(
    config: PersonaRuntimeConfig,
    chatbo_profile: dict[str, Any] | None,
) -> PersonaRuntimeConfig:
    if not chatbo_profile:
        return config
    from .persona_knowledge_repository import _tone_label

    updates: dict[str, Any] = {}
    name = str(chatbo_profile.get("name") or "").strip()
    if name:
        updates["display_name"] = name
        # Prefer short call-name ("Crono") from "Crono New Store".
        updates["agent_display_name"] = name.split()[0] or name
    greeting = str(chatbo_profile.get("greeting") or "").strip()
    if greeting:
        updates["greeting_text"] = greeting
    closing = str(chatbo_profile.get("closing_message") or "").strip()
    if closing:
        updates["closing_message"] = closing
    tone = _tone_label(chatbo_profile.get("tone"))
    if tone:
        updates["tone"] = tone
    tone_details = str(chatbo_profile.get("tone_details") or "").strip()
    if tone_details:
        updates["tone_details"] = tone_details
    address = str(chatbo_profile.get("customer_address_style") or "").strip()
    if address:
        updates["customer_address_style"] = address
    qualification_prompts = _as_prompt_list(chatbo_profile.get("qualification_rules"))
    if qualification_prompts:
        updates["qualification_prompts"] = qualification_prompts
        # ChatBo qualification list is an executable discovery gate, not just prose.
        updates["require_qualification_before_catalog"] = True
    recommendation = parse_recommendation_policy(
        chatbo_profile.get("recommendation_rules")
    )
    if recommendation.get("recommendation_rule_texts"):
        updates.update(recommendation)
    objections = _as_prompt_list(chatbo_profile.get("objection_handling"))
    if objections:
        updates["objection_prompts"] = objections
    goals = _as_prompt_list(chatbo_profile.get("sales_goals"))
    if goals:
        updates["sales_goal_prompts"] = goals
    examples = _as_prompt_list(chatbo_profile.get("examples"))
    if examples:
        updates["example_prompts"] = examples
    if not updates:
        return config
    enriched = config.model_copy(update=updates)
    return enriched.bind_sources(
        active_persona=config.active_persona,
        chatbo_profile=chatbo_profile,
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
    config = _enrich_from_chatbo_profile(config, chatbo_profile)
    config.chatbo_persona_id = chatbo_persona_id(active.metadata)

    meta_policy = _metadata_policy(active.metadata)
    if meta_policy:
        config = apply_policy_overrides(config, meta_policy, source="metadata")
        config = _enrich_from_chatbo_profile(config, chatbo_profile)
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
                "tone_details",
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
        config = _enrich_from_chatbo_profile(config, chatbo_profile)
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
