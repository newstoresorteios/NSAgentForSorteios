"""Deterministic ChatBo objection handlers (price, lead time, trust, discount, trade-in)."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from app.commerce.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.persona.persona_runtime import (
    DEFAULT_PIX_DISCOUNT_PERCENT,
    get_persona_runtime,
)

ObjectionKind = Literal[
    "extra_discount",
    "price",
    "lead_time",
    "trust",
    "comparison",
    "approval",
    "trade_in",
]


def _fold(text: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _policy() -> dict[str, Any]:
    runtime = get_persona_runtime()
    if runtime is None:
        return {
            "pix_discount_percent": DEFAULT_PIX_DISCOUNT_PERCENT,
            "max_pix_discount_percent": DEFAULT_PIX_DISCOUNT_PERCENT,
            "negotiation_beyond_pix": "human_handoff",
            "objection_prompts": [],
            "policy_source": "defaults",
        }
    data = runtime.flow_params_dict()
    data["objection_prompts"] = list(runtime.objection_prompts or [])
    return data


def _scripts_by_kind(prompts: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    labels = {
        "preço": "price",
        "preco": "price",
        "prazo": "lead_time",
        "confiança": "trust",
        "confianca": "trust",
        "comparação": "comparison",
        "comparacao": "comparison",
        "necessidade de aprovação": "approval",
        "necessidade de aprovacao": "approval",
        "aprovação": "approval",
        "aprovacao": "approval",
    }
    for raw in prompts:
        text = str(raw or "").strip()
        if not text:
            continue
        head, _, rest = text.partition(":")
        key = _fold(head).strip()
        kind = labels.get(key)
        if kind:
            mapping[kind] = (rest or text).strip()
        else:
            # Unlabeled script — keep under generic bucket by keywords.
            folded = _fold(text)
            if "desconto além" in folded or "desconto alem" in folded:
                mapping.setdefault("extra_discount", text)
            elif "pronta entrega" in folded or "sob encomenda" in folded:
                mapping.setdefault("lead_time", text)
            elif "preço do site" in folded or "preco do site" in folded:
                mapping.setdefault("price", text)
    return mapping


def detect_objection_kind(text: str | None) -> ObjectionKind | None:
    folded = _fold(text)
    if not folded.strip():
        return None

    from app.verify.guardrails import detect_trade_in_or_appraisal_request

    if detect_trade_in_or_appraisal_request(text):
        return "trade_in"

    if re.search(
        r"(mais\s+de\s+\d+\s*%|\d+\s*%\s*(de\s+)?desconto|desconto\s+maior|"
        r"alem\s+dos?\s+\d+|além\s+dos?\s+\d+|faz\s+\d+\s*%|me\s+da\s+\d+\s*%|"
        r"abaixa\s+o\s+preco|abaixa\s+o\s+preço|negocia(r)?\s+o\s+preco|"
        r"negocia(r)?\s+o\s+preço|cobertura\s+de\s+preco|cobertura\s+de\s+preço|"
        r"\b(tem|quero|pedi|faz|da|d[aá])\s+(um\s+)?desconto\b|"
        r"\bdesconto\s+no\s+pix\b|\bdesconto\s+pix\b)",
        folded,
    ):
        return "extra_discount"

    if any(
        token in folded
        for token in (
            "ta caro",
            "está caro",
            "esta caro",
            "muito caro",
            "achei caro",
            "sai caro",
            "fora do orcamento",
            "fora do orçamento",
            "nao cabe no bolso",
            "não cabe no bolso",
            "tem mais barato",
            "opcao mais barata",
            "opção mais barata",
        )
    ):
        return "price"

    if any(
        token in folded
        for token in (
            "demora muito",
            "muito tempo",
            "quando chega",
            "qual o prazo",
            "prazo de entrega",
            "quanto tempo demora",
            "chega rapido",
            "chega rápido",
            "preciso pra data",
            "preciso para data",
            "urgente",
            "tem pronta entrega",
        )
    ):
        return "lead_time"

    if any(
        token in folded
        for token in (
            "e confiavel",
            "é confiavel",
            "e confiável",
            "é confiável",
            "posso confiar",
            "golpe",
            "fraude",
            "nota fiscal",
            "garantia de verdade",
            "loja seria",
            "voces sao confiaveis",
            "vocês são confiáveis",
        )
    ):
        return "trust"

    if any(
        token in folded
        for token in (
            "vi mais barato",
            "outro site",
            "concorrente",
            "na amazon",
            "no mercado livre",
            "la fora",
            "lá fora",
            "fora do brasil",
        )
    ):
        return "comparison"

    if any(
        token in folded
        for token in (
            "preciso falar com",
            "vou conversar com",
            "minha esposa",
            "meu marido",
            "meu socio",
            "meu sócio",
            "e presente",
            "é presente",
            "deixa eu ver com",
            "depois eu confirmo",
        )
    ):
        return "approval"

    return None


def _short_reply_for_kind(
    kind: ObjectionKind,
    *,
    pix_pct: int,
    negotiation: str,
    script: str | None,
) -> tuple[str, bool]:
    """Return (reply_text, handoff_required). Prefer short WhatsApp copy over full ChatBo essay."""
    beyond = (
        "posso te conectar com um consultor humano"
        if negotiation == "human_handoff"
        else "não consigo negociar além disso"
    )
    if kind == "trade_in":
        return (
            "A New Store não avalia nem compra relógios de particulares por aqui. "
            "Se quiser, te passo para um consultor humano ver o melhor caminho.",
            True,
        )
    if kind == "extra_discount":
        return (
            f"O desconto oficial no PIX é de {pix_pct}% sobre o valor do site — "
            f"não aplico além disso. Se precisar negociar, {beyond}.",
            negotiation == "human_handoff",
        )
    if kind == "price":
        return (
            f"O valor do site já é o preço final (impostos, NF e entrega inclusos). "
            f"No PIX sai com {pix_pct}% de desconto. Se estiver acima do orçamento, "
            "me diga a faixa que eu busco opções semelhantes ou Open Box, quando houver.",
            False,
        )
    if kind == "lead_time":
        return (
            "O prazo depende da modalidade confirmada no catálogo: "
            "pronta entrega em cerca de 2 a 5 dias úteis, "
            "ou sob encomenda em cerca de 25 a 35 dias úteis. "
            "Se você tem data-alvo, priorizo as opções de pronta entrega.",
            False,
        )
    if kind == "trust":
        return (
            "Faz sentido essa preocupação numa compra de valor. "
            "A New Store importa com NF brasileira no seu nome, garantia "
            "(fabricante ou nossa) e impostos já recolhidos. "
            "Se quiser, te coloco com o João da equipe.",
            False,
        )
    if kind == "comparison":
        return (
            f"Não falo mal de outros anúncios. Aqui o preço já inclui importação, "
            f"impostos, NF e entrega — no PIX ainda tem {pix_pct}%. "
            f"Se precisar de cobertura de preço, {beyond}.",
            negotiation == "human_handoff",
        )
    if kind == "approval":
        return (
            "Sem pressão. Posso deixar pronto um resumo com modelo, valor no PIX/"
            "parcelado, prazo e o link oficial para você alinhar com quem precisar.",
            False,
        )
    # Fallback: first sentence of persona script if present.
    if script:
        first = re.split(r"(?<=[.!?])\s+", script.strip())[0].strip()
        if first:
            return first[:400], False
    return (
        "Posso te explicar com base na política oficial da New Store. "
        "O que mais te preocupa agora: preço, prazo ou confiança?",
        False,
    )


def objection_policy_result(
    kind: ObjectionKind,
    *,
    state: CommerceConversationState | None = None,
) -> AgentResult:
    policy = _policy()
    pix_pct = int(policy.get("pix_discount_percent") or DEFAULT_PIX_DISCOUNT_PERCENT)
    max_pix = int(policy.get("max_pix_discount_percent") or pix_pct)
    negotiation = str(policy.get("negotiation_beyond_pix") or "human_handoff")
    scripts = _scripts_by_kind(list(policy.get("objection_prompts") or []))
    script = scripts.get(kind) or scripts.get("extra_discount" if kind == "extra_discount" else kind)
    reply, handoff = _short_reply_for_kind(
        kind,
        pix_pct=pix_pct,
        negotiation=negotiation,
        script=script,
    )
    products = []
    if state is not None:
        products = [
            item.model_dump(mode="json")
            for item in state.last_presented_products[:3]
        ]
    return AgentResult(
        reply_text=reply,
        intent="commerce" if not handoff else "handoff",
        handoff_required=handoff,
        safety_reason=f"objection_{kind}",
        commercial_data={
            "objection": {
                "kind": kind,
                "pix_discount_percent": pix_pct,
                "max_pix_discount_percent": max_pix,
                "negotiation_beyond_pix": negotiation,
                "script_present": bool(script),
            },
            "products": products,
        },
        response_metadata={
            "domain": "commerce",
            "response_source": "deterministic_objection",
            "objection_kind": kind,
            "persona_runtime": {
                "policy_source": policy.get("policy_source"),
                "persona_version_id": policy.get("persona_version_id"),
            },
        },
    )


def try_objection_authority_result(
    message: IncomingMessage,
    interpretation: SalesInterpretation | None,
    state: CommerceConversationState,
) -> AgentResult | None:
    """Early deterministic objection path; None = continue normal sales flow."""
    kind = detect_objection_kind(message.text)
    if kind is None:
        return None

    # Discount policy must win over checkout orchestration (persona P0).
    # Informational PIX/payment questions keep the payment policy path so we
    # do not create a cart or collide with require_cart_for_informational_payment.
    if kind == "extra_discount":
        if (
            interpretation is not None
            and interpretation.payment_request_kind == "informational"
        ):
            return None
        return objection_policy_result(kind, state=state)

    # Don't steal active checkout / payment orchestration turns.
    if interpretation is not None:
        if interpretation.checkout_data is not None:
            return None
        if interpretation.shipping_action:
            return None
        if interpretation.payment_request_kind in {"checkout", "informational"}:
            return None
        if interpretation.confirmation in {"confirm", "reject"}:
            return None
        if interpretation.purchase_action in {"create_cart", "checkout_question", "show_cart_link"}:
            return None

    return objection_policy_result(kind, state=state)
