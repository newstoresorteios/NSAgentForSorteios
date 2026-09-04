"""Answer council: dual inbound, query check, dual outbound, judge, one restart.

Deterministic checkers always run. They must both pass. On fail, research
restarts once with a correction packet (not a second invented shortlist).
LLM critique/judge remain downstream for prose; they do not authorize a
constraint miss.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.commerce.commerce_context import CommerceConversationState
from ..config import get_settings
from app.identity.greeting_policy import is_generic_greeting_reply
from ..models import AgentResult, IncomingMessage, SalesInterpretation
from app.catalog.product_retrieval import effective_price
from .turn_contract import (
    TurnContract,
    inbound_from_memory,
    inbound_from_message,
    merge_inbound_views,
    reply_claims_checkout,
    reply_claims_occasion,
)

_FRESH_LIST_RE = re.compile(
    r"encontrei|separei|mais pr[oó]ximos|estas \d+ op(?:ç|c)(?:õ|o)es",
    re.IGNORECASE,
)
_REQUALIFY_RE = re.compile(
    r"(como posso te chamar|seu nome|te chamar|"
    r"para qual cidade|sua cidade|"
    r"para (que|qual) uso|qual o uso|qual a ocasi)",
    re.IGNORECASE,
)
_MUST_RETRIEVE_CODES = frozenset(
    {
        "enforce_budget",
        "forbid_near_match",
        "enforce_color",
        "enforce_gender",
        "enforce_style",
        "model_lock",
    }
)
_SKIP_RETRIEVAL_CODES = frozenset(
    {"continue_commerce", "honor_sku_lock", "stop_requalify"}
)


class CheckerReport(BaseModel):
    name: str
    pass_check: bool = True
    issues: list[str] = Field(default_factory=list)


class CouncilDecision(BaseModel):
    approved: bool = True
    issues: list[str] = Field(default_factory=list)
    restart: bool = False
    correction_codes: list[str] = Field(default_factory=list)
    contract: dict[str, Any] = Field(default_factory=dict)
    checker_a: dict[str, Any] = Field(default_factory=dict)
    checker_b: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 1


def build_turn_contract(
    *,
    message_text: str | None,
    interpretation: SalesInterpretation | None,
    commerce_state: CommerceConversationState | None,
) -> TurnContract:
    message_view = inbound_from_message(message_text, interpretation)
    memory_view = inbound_from_memory(interpretation, commerce_state)
    return merge_inbound_views(
        message_view=message_view,
        memory_view=memory_view,
        message_text=message_text,
        interpretation=interpretation,
    )


def _presented(result: AgentResult) -> list[dict[str, Any]]:
    products = (result.commercial_data or {}).get("products") or []
    return [item for item in products if isinstance(item, dict)]


def _catalog_identity_applies(result: AgentResult, contract: TurnContract) -> bool:
    """A purchase-close of one SKU is not a shortlist to re-filter on Tray."""
    if not contract.purchase_close:
        return True
    return len(_presented(result)) >= 2


_STYLE_MATCH_RE = {
    "esportivo": re.compile(
        r"\b(esportivo|sport|diver|mergulho|khaki)\b", re.IGNORECASE
    ),
    "diver": re.compile(r"\b(diver|mergulho|aquascaphe|seascape|200m|300m)\b", re.IGNORECASE),
    "social": re.compile(
        r"\b(social|dress|elegant|dressy|prestige|le locle)\b", re.IGNORECASE
    ),
    "cronógrafo": re.compile(r"\b(crono|chrono)\b", re.IGNORECASE),
}
_STYLE_RIVAL_RE = {
    "esportivo": re.compile(r"\b(dress|social|elegant|festa|prestige)\b", re.IGNORECASE),
    "diver": re.compile(r"\b(dress|social|elegant|prestige)\b", re.IGNORECASE),
    "social": re.compile(r"\b(diver|mergulho)\b", re.IGNORECASE),
    "cronógrafo": re.compile(r"\b(quartz only)\b", re.IGNORECASE),
}


def _product_catalog_text(product: dict[str, Any]) -> str:
    from app.catalog.product_retrieval import _product_text

    return _product_text(product)


def _presented_conflicts_model(products: list[dict[str, Any]], model: str) -> bool:
    from app.catalog.product_retrieval import required_model_tokens

    tokens = required_model_tokens(model)
    if not tokens or not products:
        return False

    def _matches(product: dict[str, Any]) -> bool:
        text = _product_catalog_text(product)
        return all(token in text for token in tokens)

    return not any(_matches(item) for item in products)


def _presented_conflicts_color(products: list[dict[str, Any]], color: str) -> bool:
    from app.catalog.product_retrieval import product_conflicts_dial_color

    token = str(color or "").strip().casefold()
    if not token or not products:
        return False
    return all(product_conflicts_dial_color(item, (token,)) for item in products)


def _presented_conflicts_gender(products: list[dict[str, Any]], gender: str) -> bool:
    from app.catalog.preference_normalize import gender_search_aliases

    wanted = gender_search_aliases(gender)
    rival_label = (
        "masculino"
        if gender == "feminino"
        else ("feminino" if gender == "masculino" else None)
    )
    if not wanted or not rival_label or not products:
        return False
    rivals = gender_search_aliases(rival_label)

    def _has(text: str, tokens: tuple[str, ...]) -> bool:
        return any(re.search(rf"\b{re.escape(token)}\b", text) for token in tokens)

    def _conflicts(product: dict[str, Any]) -> bool:
        text = _product_catalog_text(product)
        return _has(text, rivals) and not _has(text, wanted)

    return all(_conflicts(item) for item in products)


def _presented_conflicts_style(products: list[dict[str, Any]], style: str) -> bool:
    key = str(style or "").strip().casefold()
    if key == "cronografo":
        key = "cronógrafo"
    match_re = _STYLE_MATCH_RE.get(key)
    rival_re = _STYLE_RIVAL_RE.get(key)
    if not match_re or not rival_re or not products:
        return False

    def _conflicts(product: dict[str, Any]) -> bool:
        text = _product_catalog_text(product)
        return bool(rival_re.search(text)) and not bool(match_re.search(text))

    return all(_conflicts(item) for item in products)


def check_pedido(result: AgentResult, contract: TurnContract) -> CheckerReport:
    """Checker A — did we answer the customer’s ask (not a nearby substitute)?"""
    issues: list[str] = []
    products = _presented(result)
    reply = result.reply_text or ""
    if contract.must_not_re_greet and is_generic_greeting_reply(reply):
        issues.append("re_greet_instead_of_commerce")
    if contract.must_not_claim_stale_occasion and reply_claims_occasion(reply):
        issues.append("stale_occasion_claimed")
    if contract.budget_max is not None:
        over = [
            item
            for item in products
            if (price := effective_price(item)) is not None
            and price > float(contract.budget_max)
        ]
        if over:
            issues.append("presented_over_budget")
        if (result.response_metadata or {}).get("guided_near_match") and products:
            issues.append("near_match_with_budget")
        if contract.asks_price_range and over:
            issues.append("ignored_stated_range")
    interp = (result.response_metadata or {}).get("interpretation")
    recipient = ""
    if isinstance(interp, dict):
        prefs = interp.get("preferences") or {}
        recipient = str(prefs.get("recipient") or "")
    from .qualification_slots import _is_plausible_name

    if recipient and not _is_plausible_name(recipient):
        issues.append("commerce_phrase_used_as_name")
    if contract.purchase_close and products and len(products) >= 2:
        if (result.response_metadata or {}).get("guided_near_match") or _FRESH_LIST_RE.search(
            reply
        ):
            issues.append("reopened_discovery_on_purchase_close")
    if (contract.sku_lock or contract.live_shortlist) and _REQUALIFY_RE.search(reply):
        issues.append("requalify_after_sku")
    if contract.must_not_claim_stale_checkout and reply_claims_checkout(reply):
        issues.append("claimed_stale_checkout")
    if products and _catalog_identity_applies(result, contract):
        if contract.color and _presented_conflicts_color(products, contract.color):
            issues.append("ignored_color")
        if contract.model and _presented_conflicts_model(products, contract.model):
            issues.append("ignored_model")
        if contract.gender and _presented_conflicts_gender(products, contract.gender):
            issues.append("ignored_gender")
        if contract.style and _presented_conflicts_style(products, contract.style):
            issues.append("ignored_style")
    return CheckerReport(name="pedido", pass_check=not issues, issues=issues)


def check_fatos(result: AgentResult, contract: TurnContract) -> CheckerReport:
    """Checker B — presented SKUs/prices must be catalog facts inside the contract."""
    issues: list[str] = []
    products = _presented(result)
    if result.safety_reason == "factual_validation_failed" and products:
        issues.append("factual_failed_but_still_listing")
    if contract.budget_max is not None:
        for item in products:
            price = effective_price(item)
            if price is not None and price > float(contract.budget_max):
                issues.append("fact_price_over_budget")
                break
    brand = (contract.brand or "").strip().casefold()
    identity = _catalog_identity_applies(result, contract)
    if identity and brand and products:
        labels = [str(item.get("brand") or "").strip().casefold() for item in products]
        if labels and all(label and brand not in label and label not in brand for label in labels):
            issues.append("fact_brand_mismatch")
    if products and identity:
        if contract.color and _presented_conflicts_color(products, contract.color):
            issues.append("fact_color_mismatch")
        if contract.model and _presented_conflicts_model(products, contract.model):
            issues.append("fact_model_mismatch")
        if contract.gender and _presented_conflicts_gender(products, contract.gender):
            issues.append("fact_gender_mismatch")
        if contract.style and _presented_conflicts_style(products, contract.style):
            issues.append("fact_style_mismatch")
    if contract.must_not_claim_stale_checkout:
        data = result.commercial_data or {}
        if data.get("cart") or data.get("checkout"):
            issues.append("fact_stale_checkout")
    return CheckerReport(name="fatos", pass_check=not issues, issues=issues)


def judge_council(
    checker_a: CheckerReport,
    checker_b: CheckerReport,
    *,
    attempt: int,
    max_restarts: int,
) -> CouncilDecision:
    issues = list(dict.fromkeys([*checker_a.issues, *checker_b.issues]))
    approved = checker_a.pass_check and checker_b.pass_check
    restart = (not approved) and attempt <= max_restarts
    codes: list[str] = []
    if "presented_over_budget" in issues or "fact_price_over_budget" in issues:
        codes.append("enforce_budget")
    if "near_match_with_budget" in issues or "ignored_stated_range" in issues:
        codes.append("forbid_near_match")
    if "stale_occasion_claimed" in issues:
        codes.append("drop_stale_occasion")
    if "re_greet_instead_of_commerce" in issues:
        codes.append("continue_commerce")
    if "commerce_phrase_used_as_name" in issues:
        codes.append("clear_fake_name")
    if "reopened_discovery_on_purchase_close" in issues:
        codes.append("honor_sku_lock")
    if "requalify_after_sku" in issues:
        codes.append("stop_requalify")
    if "ignored_color" in issues or "fact_color_mismatch" in issues:
        codes.append("enforce_color")
        codes.append("forbid_near_match")
    if "ignored_model" in issues or "fact_model_mismatch" in issues:
        codes.append("model_lock")
        codes.append("forbid_near_match")
    if "ignored_gender" in issues or "fact_gender_mismatch" in issues:
        codes.append("enforce_gender")
    if "ignored_style" in issues or "fact_style_mismatch" in issues:
        codes.append("enforce_style")
    if "claimed_stale_checkout" in issues or "fact_stale_checkout" in issues:
        codes.append("drop_stale_checkout")
    return CouncilDecision(
        approved=approved,
        issues=issues,
        restart=restart,
        correction_codes=codes,
        checker_a=checker_a.model_dump(),
        checker_b=checker_b.model_dump(),
        attempts=attempt,
    )


def apply_corrections(
    interpretation: SalesInterpretation,
    contract: TurnContract,
    codes: list[str],
) -> SalesInterpretation:
    updated = interpretation.model_copy(deep=True)
    prefs = updated.preferences
    if "drop_stale_budget" in codes:
        prefs.budget_max = None
        prefs.budget_min = None
    if "enforce_budget" in codes and contract.budget_max is not None:
        prefs.budget_max = contract.budget_max
    if "drop_stale_occasion" in codes:
        prefs.occasion = None
    if "drop_stale_color" in codes:
        prefs.color = None
    if "drop_stale_style" in codes:
        prefs.style = None
    if "drop_stale_gender" in codes:
        from app.catalog.preference_normalize import detect_gender_label

        if detect_gender_label(prefs.recipient) and not _looks_like_person_name(
            prefs.recipient
        ):
            prefs.recipient = None
        prefs.attributes = [
            item
            for item in (prefs.attributes or [])
            if detect_gender_label(item) is None
        ]
    if "enforce_color" in codes and contract.color:
        prefs.color = contract.color
    if "enforce_style" in codes and contract.style:
        prefs.style = contract.style
    if "enforce_gender" in codes and contract.gender:
        from app.catalog.preference_normalize import detect_gender_label

        prefs.recipient = contract.gender
        attrs = [
            item
            for item in (prefs.attributes or [])
            if detect_gender_label(item) is None
        ]
        attrs.append(contract.gender)
        prefs.attributes = attrs
    if "clear_fake_name" in codes:
        from app.catalog.preference_normalize import detect_gender_label

        if not _looks_like_person_name(prefs.recipient) and not detect_gender_label(
            prefs.recipient
        ):
            prefs.recipient = None
        attrs = [
            item
            for item in (prefs.attributes or [])
            if not str(item).startswith("qual:name:")
        ]
        prefs.attributes = attrs
    if "continue_commerce" in codes:
        updated.domain = "commerce"
        if updated.goal is None:
            updated.goal = "discover"
        if not (updated.subject.product_type or "").strip():
            updated.subject = updated.subject.model_copy(
                update={"product_type": "relógio"}
            )
        updated.references_previous_context = True
        updated.needs_clarification = False
    if "brand_lock" in codes and contract.brand:
        if not (updated.subject.brand or "").strip():
            updated.subject = updated.subject.model_copy(
                update={"brand": contract.brand}
            )
    if "model_lock" in codes and contract.model:
        updates: dict[str, Any] = {}
        if not (updated.subject.model or "").strip():
            updates["model"] = contract.model
        if contract.brand and not (updated.subject.brand or "").strip():
            updates["brand"] = contract.brand
        if updates:
            updated.subject = updated.subject.model_copy(update=updates)
    if "honor_sku_lock" in codes:
        updated.goal = "buy"
        updated.stop_clarification = True
        updated.needs_clarification = False
        updated.references_previous_context = True
    if "stop_requalify" in codes:
        updated.stop_clarification = True
        updated.needs_clarification = False
        updated.clarification_question = None
        updated.references_previous_context = True
    if "drop_stale_checkout" in codes:
        updated.purchase_action = None
        if updated.goal == "buy" and not contract.purchase_close:
            updated.goal = "discover"
        updated.references_previous_context = True
    updated.preferences = prefs
    skip_search = (
        "honor_sku_lock" in codes
        and "enforce_budget" not in codes
        and "forbid_near_match" not in codes
    )
    was_paused = (not interpretation.ready_for_retrieval) and (
        interpretation.goal == "buy"
        or bool(interpretation.purchase_action)
        or contract.purchase_close
    )
    released_from_stale_checkout = "drop_stale_checkout" in codes and bool(
        (contract.brand or "").strip()
    )
    if (was_paused or skip_search) and not released_from_stale_checkout:
        updated.ready_for_retrieval = False
    else:
        updated.ready_for_retrieval = True
        updated.enough_information_to_search = True
    if "forbid_near_match" in codes:
        updated._forbid_near_match = True
        from .discovery import _specific_product_lock

        keep_exact = bool(
            contract.sku_lock
            or (contract.model or "").strip()
            or _specific_product_lock(updated)
        )
        if not keep_exact:
            updated._force_recommendation_mode = True
    return updated


def pre_search_correction_codes(
    contract: TurnContract,
    interpretation: SalesInterpretation | None = None,
) -> list[str]:
    """Codes the organizer must apply before the first catalog search."""
    codes: list[str] = []
    if "budget" in contract.stale_fields:
        codes.append("drop_stale_budget")
    elif contract.budget_max is not None:
        codes.append("enforce_budget")
        codes.append("forbid_near_match")
    elif contract.asks_price_range:
        codes.append("forbid_near_match")
    if contract.must_not_claim_stale_occasion or "occasion" in contract.stale_fields:
        codes.append("drop_stale_occasion")
    if "color" in contract.stale_fields:
        codes.append("drop_stale_color")
    elif contract.color:
        codes.append("enforce_color")
        codes.append("forbid_near_match")
    if "style" in contract.stale_fields:
        codes.append("drop_stale_style")
    elif contract.style:
        codes.append("enforce_style")
    if "gender" in contract.stale_fields:
        codes.append("drop_stale_gender")
    elif contract.gender:
        codes.append("enforce_gender")
    if "checkout" in contract.stale_fields:
        codes.append("drop_stale_checkout")
    if interpretation is not None:
        recipient = interpretation.preferences.recipient
        if recipient and not _looks_like_person_name(recipient):
            codes.append("clear_fake_name")
        if contract.brand and not (interpretation.subject.brand or "").strip():
            codes.append("brand_lock")
        if contract.model and not (interpretation.subject.model or "").strip():
            codes.append("model_lock")
    elif contract.model:
        codes.append("model_lock")
    return list(dict.fromkeys(codes))


def apply_turn_contract_for_search(
    interpretation: SalesInterpretation,
    *,
    message_text: str | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> SalesInterpretation:
    """Bind dual-inbound contract onto the interpretation used to search Tray."""
    from .tray_refresh import excluded_product_ids_for_turn

    if getattr(interpretation, "_turn_contract_bound", False):
        interpretation._excluded_product_ids = excluded_product_ids_for_turn(
            interpretation, message_text, commerce_state
        )
        return interpretation
    contract = build_turn_contract(
        message_text=message_text,
        interpretation=interpretation,
        commerce_state=commerce_state,
    )
    codes = pre_search_correction_codes(contract, interpretation)
    updated = (
        apply_corrections(interpretation, contract, codes) if codes else interpretation
    )
    updated._turn_contract_bound = True
    updated._excluded_product_ids = excluded_product_ids_for_turn(
        updated, message_text, commerce_state
    )
    print(
        "[sales.turn_contract.bind]",
        {
            "codes": codes,
            "budget_max": updated.preferences.budget_max,
            "brand": updated.subject.brand,
            "model": updated.subject.model,
            "occasion": updated.preferences.occasion,
            "color": updated.preferences.color,
            "style": updated.preferences.style,
            "stale_fields": contract.stale_fields,
            "live_checkout": contract.live_checkout,
            "forbid_near_match": bool(
                getattr(updated, "_forbid_near_match", False)
            ),
        },
    )
    return updated


def _looks_like_person_name(value: str | None) -> bool:
    from .qualification_slots import _is_plausible_name

    return _is_plausible_name(str(value or ""))


def _should_retrieve_on_restart(
    codes: list[str],
    *,
    contract: TurnContract | None = None,
) -> bool:
    code_set = set(codes)
    if code_set & _SKIP_RETRIEVAL_CODES:
        return False
    if contract is not None and contract.purchase_close:
        return False
    if code_set & _MUST_RETRIEVE_CODES:
        return True
    if "drop_stale_checkout" in code_set:
        return bool(contract and (contract.brand or "").strip())
    return True


def _continue_commerce_reply(
    commerce_state: CommerceConversationState | None,
    contract: TurnContract,
) -> AgentResult:
    """Resume the live sale instead of greeting or dumping a new shortlist."""
    from .purchase_selection import parse_list_position_selection

    if "checkout" in contract.stale_fields:
        return AgentResult(
            reply_text=(
                "Pode seguir — me diz a marca, a faixa de investimento "
                "ou o modelo que você tem em mente."
            ),
            intent="commerce",
            safety_reason="commerce_clarification",
            response_metadata={
                "domain": "commerce",
                "presented_products": False,
                "answer_council_continue": True,
                "drop_stale_checkout": True,
            },
        )
    presented = list(getattr(commerce_state, "last_presented_products", None) or [])
    position = parse_list_position_selection(contract.asked_text)
    if contract.purchase_close and position and presented:
        match = next(
            (item for item in presented if item.position == position),
            None,
        )
        if match is None and 1 <= position <= len(presented):
            match = presented[position - 1]
        if match is not None:
            name = match.name or f"opção {position}"
            product = {
                "id": match.product_id,
                "product_id": match.product_id,
                "name": match.name,
                "brand": match.brand,
                "reference": match.reference,
                "product_url": match.product_url,
                "url": match.product_url,
            }
            return AgentResult(
                reply_text=(
                    f"Fechamos na opção {position} — {name}. "
                    "Prefere PIX, cartão ou o link do site para pagar?"
                ),
                intent="commerce",
                safety_reason="commerce_clarification",
                commercial_data={"products": [product]},
                response_metadata={
                    "domain": "commerce",
                    "presented_products": True,
                    "answer_council_continue": True,
                    "honor_sku_lock": True,
                },
            )
    if presented and commerce_state is not None:
        from app.memory.context_resume import build_presented_catalog_resume_result

        resume = build_presented_catalog_resume_result(commerce_state)
        if resume is not None:
            metadata = dict(resume.response_metadata or {})
            metadata["answer_council_continue"] = True
            resume.response_metadata = metadata
            if contract.purchase_close:
                resume.reply_text = (
                    "Ainda estou com as opções que te mostrei. "
                    "Qual você quer fechar — a 1, 2 ou 3?"
                )
            elif not str(resume.reply_text or "").startswith("Ainda estou"):
                resume.reply_text = (
                    "Ainda estou com as opções que te mostrei:\n\n"
                    + str(resume.reply_text or "").split(":\n\n", 1)[-1]
                )
            return resume
    return AgentResult(
        reply_text=(
            "Pode seguir — me diz a marca, a faixa de investimento "
            "ou o modelo que você tem em mente."
        ),
        intent="commerce",
        safety_reason="commerce_clarification",
        response_metadata={
            "domain": "commerce",
            "presented_products": False,
            "answer_council_continue": True,
        },
    )


def _fallback_blocked_reply(
    result: AgentResult,
    contract: TurnContract,
    interpretation: SalesInterpretation | None,
    commerce_state: CommerceConversationState | None,
    codes: list[str],
) -> AgentResult:
    code_set = set(codes)
    if (code_set & _SKIP_RETRIEVAL_CODES) and not (code_set & _MUST_RETRIEVE_CODES):
        return _continue_commerce_reply(commerce_state, contract)
    return _honest_constraint_reply(result, contract, interpretation)


def _honest_constraint_reply(
    result: AgentResult,
    contract: TurnContract,
    interpretation: SalesInterpretation | None,
) -> AgentResult:
    from .tray_query_authority import budget_hard_miss_result

    if contract.budget_max is not None and interpretation is not None:
        miss = budget_hard_miss_result(
            interpretation.model_copy(
                update={
                    "preferences": interpretation.preferences.model_copy(
                        update={"budget_max": contract.budget_max}
                    )
                }
            ),
            _presented(result),
        )
        if miss is not None:
            return miss
    fixed = result.model_copy(deep=True)
    commercial = dict(fixed.commercial_data or {})
    commercial["products"] = []
    fixed.commercial_data = commercial
    if contract.budget_max is not None and contract.brand:
        ceiling = f"R$ {contract.budget_max:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        fixed.reply_text = (
            f"Não encontrei {contract.brand} até {ceiling}. "
            "Prefere outra marca nessa faixa, ou subir o orçamento?"
        )
    elif contract.color:
        label = contract.brand or "relógio"
        fixed.reply_text = (
            f"Não encontrei {label} na cor {contract.color} com o que você pediu. "
            "Prefere outra cor, ou outra marca?"
        )
    elif contract.gender:
        label = contract.brand or "relógio"
        fixed.reply_text = (
            f"Não encontrei {label} {contract.gender} com o que você pediu. "
            "Quer ajustar gênero, marca ou faixa?"
        )
    elif contract.style:
        label = contract.brand or "relógio"
        fixed.reply_text = (
            f"Não encontrei {label} no estilo {contract.style} com o que você pediu. "
            "Prefere outro estilo, ou outra marca?"
        )
    else:
        fixed.reply_text = (
            "Não fechei uma opção que atenda o que você pediu agora. "
            "Quer ajustar marca ou faixa de investimento?"
        )
    fixed.safety_reason = "answer_council_blocked"
    metadata = dict(fixed.response_metadata or {})
    metadata["presented_products"] = False
    metadata["guided_near_match"] = False
    metadata["hard_budget_max"] = contract.budget_max
    fixed.response_metadata = metadata
    return fixed


def _attach_decision(result: AgentResult, decision: CouncilDecision) -> AgentResult:
    metadata = dict(result.response_metadata or {})
    metadata["answer_council"] = decision.model_dump(mode="json")
    result.response_metadata = metadata
    return result


def _stamp_stale_checkout_clear(
    result: AgentResult,
    contract: TurnContract,
    commerce_state: CommerceConversationState | None,
) -> AgentResult:
    """Wipe leftover cart identity when this turn started a new browse."""
    if "checkout" not in contract.stale_fields:
        return result
    from app.commerce.cart_service import _clear_cart_session_state
    from app.memory.context_resume import session_has_unpaid_order

    metadata = dict(result.response_metadata or {})
    metadata["domain"] = metadata.get("domain") or "commerce"
    metadata["drop_stale_checkout"] = True
    metadata["dialogue_phase"] = "discovery"
    if commerce_state is not None and not session_has_unpaid_order(commerce_state):
        metadata["dialogue_phase_reset"] = True
        metadata["clear_pending_action"] = True
        metadata["clear_active_product"] = True
        metadata["cart_state"] = _clear_cart_session_state(commerce_state)
    result.response_metadata = metadata
    return result


def _finish_council(
    result: AgentResult,
    decision: CouncilDecision,
    interpretation: SalesInterpretation | None,
    contract: TurnContract,
    commerce_state: CommerceConversationState | None,
) -> tuple[AgentResult, CouncilDecision, SalesInterpretation | None]:
    attached = _stamp_stale_checkout_clear(
        _attach_decision(result, decision),
        contract,
        commerce_state,
    )
    return attached, decision, interpretation


async def apply_answer_council_with_retry(
    result: AgentResult,
    *,
    incoming: IncomingMessage,
    interpretation: SalesInterpretation | None,
    commerce_state: CommerceConversationState | None = None,
) -> tuple[AgentResult, CouncilDecision, SalesInterpretation | None]:
    settings = get_settings()
    if not bool(getattr(settings, "agent_answer_council_enabled", True)):
        empty = CouncilDecision(approved=True, attempts=0)
        return result, empty, interpretation

    max_restarts = int(getattr(settings, "agent_answer_council_max_restarts", 1) or 1)
    current = result
    current_interp = interpretation
    attempt = 1
    decision = CouncilDecision(approved=True)
    while True:
        contract = build_turn_contract(
            message_text=incoming.text,
            interpretation=current_interp,
            commerce_state=commerce_state,
        )
        checker_a = check_pedido(current, contract)
        checker_b = check_fatos(current, contract)
        decision = judge_council(
            checker_a,
            checker_b,
            attempt=attempt,
            max_restarts=max_restarts,
        )
        decision.contract = contract.model_dump()
        print(
            "[sales.answer_council]",
            {
                "attempt": attempt,
                "approved": decision.approved,
                "issues": decision.issues[:8],
                "restart": decision.restart,
            },
        )
        if decision.approved:
            return _finish_council(
                current, decision, current_interp, contract, commerce_state
            )
        if not decision.restart:
            blocked = _fallback_blocked_reply(
                current,
                contract,
                current_interp,
                commerce_state,
                decision.correction_codes,
            )
            decision.approved = False
            return _finish_council(
                blocked, decision, current_interp, contract, commerce_state
            )

        if current_interp is not None:
            current_interp = apply_corrections(
                current_interp, contract, decision.correction_codes
            )
            current_interp._turn_contract_bound = False
        if not _should_retrieve_on_restart(
            decision.correction_codes, contract=contract
        ):
            current = _continue_commerce_reply(commerce_state, contract)
            attempt += 1
            continue
        if current_interp is None:
            blocked = _fallback_blocked_reply(
                current,
                contract,
                current_interp,
                commerce_state,
                decision.correction_codes,
            )
            decision.approved = False
            return _finish_council(
                blocked, decision, current_interp, contract, commerce_state
            )
        try:
            from .product_lookup import execute_compiled_product_retrieval

            retry_result = await execute_compiled_product_retrieval(
                current_interp,
                message_text=incoming.text,
                commerce_state=commerce_state,
            )
        except Exception:
            blocked = _fallback_blocked_reply(
                current,
                contract,
                current_interp,
                commerce_state,
                decision.correction_codes,
            )
            return _finish_council(
                blocked, decision, current_interp, contract, commerce_state
            )
        if retry_result is None:
            blocked = _fallback_blocked_reply(
                current,
                contract,
                current_interp,
                commerce_state,
                decision.correction_codes,
            )
            return _finish_council(
                blocked, decision, current_interp, contract, commerce_state
            )
        current = retry_result
        if (
            "continue_commerce" in decision.correction_codes
            and is_generic_greeting_reply(current.reply_text)
        ):
            current = _continue_commerce_reply(commerce_state, contract)
        attempt += 1
