"""Deterministic outbound compliance checks (preference fit + dead links).

Runs before send as a lightweight "juiz" without an extra LLM call.
When preference shortlists miss dial/case constraints, attempt an in-memory
re-rank/re-search over presented products before shipping the reply.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.catalog.product_retrieval import (
    preference_case_finish_tokens,
    preference_color_tokens,
    prefer_dial_and_case_matches,
    product_matches_case_finish_tokens,
    product_matches_color_tokens,
)


class ComplianceVerdict(BaseModel):
    pass_check: bool = True
    score: int = Field(default=100, ge=0, le=100)
    issues: list[str] = Field(default_factory=list)
    summary: str = ""


class ComplianceReport(BaseModel):
    triggered: bool = False
    mode: Literal["off", "shadow", "enforce"] = "enforce"
    verdict: ComplianceVerdict | None = None
    applied: bool = False
    reresearch_applied: bool = False


_URL_RE = re.compile(r"https?://[^\s)>\]]+", flags=re.IGNORECASE)
_PERSONA_LEAK_RE = re.compile(
    r"\b(como (uma )?ia|sou (um )?modelo de linguagem|openai|gpt-?\d)\b",
    flags=re.IGNORECASE,
)


def _fold(value: Any) -> str:
    return str(value or "").strip().casefold()


def _presented_products(result: AgentResult) -> list[dict[str, Any]]:
    commercial = result.commercial_data or {}
    products = commercial.get("products") or []
    return [p for p in products if isinstance(p, dict)]


def _format_preference_shortlist(products: list[dict[str, Any]]) -> str:
    from app.commerce.commerce_router import _product_lines

    lines = _product_lines(products[:3], compact=True)
    numbered = [f"{index}. {line}" for index, line in enumerate(lines, start=1)]
    return "\n".join(numbered)


def _reresearch_preference_shortlist(
    *,
    result: AgentResult,
    interpretation: SalesInterpretation,
    issues: set[str],
) -> tuple[AgentResult, bool]:
    """Rebuild shortlist from preference-compatible products when possible."""
    products = _presented_products(result)
    if not products or interpretation is None:
        return result, False
    ranked = prefer_dial_and_case_matches(products, interpretation, limit=3)
    if not ranked:
        return result, False

    color_tokens = preference_color_tokens(interpretation)
    case_tokens = preference_case_finish_tokens(interpretation)
    goldish = bool({"dourado", "gold", "golden", "ouro"} & set(case_tokens))

    improved = ranked
    if goldish:
        gold_hits = [
            product
            for product in ranked
            if product_matches_case_finish_tokens(product, case_tokens)
            and (
                not color_tokens
                or product_matches_color_tokens(product, color_tokens)
            )
        ]
        if gold_hits:
            improved = gold_hits
        elif "listed_options_ignore_requested_gold" in issues:
            # No gold in the presented pool — honest ask instead of fake list.
            fixed = result.model_copy(deep=True)
            fixed.reply_text = (
                "Não fechei dourado com visor preto nessas opções. "
                "Quer que eu busque só as combinações dourado + mostrador preto?"
            )
            fixed.safety_reason = "compliance_preference_reresearch"
            fixed.response_metadata = dict(fixed.response_metadata or {})
            fixed.response_metadata["compliance_reresearch"] = {
                "reason": "no_gold_in_presented_pool",
                "pending_hint": "search_gold_black_dial",
            }
            return fixed, True
        else:
            return result, False
    elif color_tokens:
        dial_hits = [
            product
            for product in ranked
            if product_matches_color_tokens(product, color_tokens)
        ]
        if not dial_hits:
            return result, False
        improved = dial_hits

    if [p.get("id") or p.get("name") for p in improved] == [
        p.get("id") or p.get("name") for p in products[: len(improved)]
    ]:
        # Same order / same set — nothing to rewrite unless false_negative text.
        if "false_negative_gold_case" not in issues:
            return result, False

    fixed = result.model_copy(deep=True)
    commercial = dict(fixed.commercial_data or {})
    commercial["products"] = improved
    fixed.commercial_data = commercial
    body = _format_preference_shortlist(improved)
    fixed.reply_text = (
        "Ajustei a lista para ficar mais perto do que você pediu "
        "(cor do visor + acabamento):\n\n"
        f"{body}\n\nQual deles mais chega perto do que você imaginou?"
    )
    fixed.safety_reason = "compliance_preference_reresearch"
    fixed.response_metadata = dict(fixed.response_metadata or {})
    fixed.response_metadata["compliance_reresearch"] = {
        "reason": "reranked_presented_products",
        "product_ids": [str(p.get("id") or "") for p in improved],
    }
    return fixed, True


def check_outbound_compliance(
    *,
    incoming: IncomingMessage | None,
    result: AgentResult,
    interpretation: SalesInterpretation | None = None,
) -> ComplianceReport:
    """Flag preference mismatches, dead storefront links, and persona leaks."""
    issues: list[str] = []
    reply = result.reply_text or ""
    products = _presented_products(result)
    text = (incoming.text if incoming else "") or ""

    # 1) Dead storefront URLs must never be offered as "link oficial".
    dead_marked = [p for p in products if p.get("_product_url_dead")]
    if dead_marked and _URL_RE.search(reply):
        issues.append("dead_product_url_in_reply")
    if result.safety_reason == "product_media_link_fallback":
        for product in products:
            url = str(product.get("url") or "")
            if product.get("_product_url_dead") or (
                url and "pagenotfound" in url.casefold()
            ):
                issues.append("photo_fallback_dead_link")
                break

    # 2) Dial / case preference fit when we claim options for that ask.
    if interpretation is not None and products:
        color_tokens = preference_color_tokens(interpretation)
        case_tokens = preference_case_finish_tokens(interpretation)
        goldish = bool({"dourado", "gold", "golden", "ouro"} & set(case_tokens))
        if color_tokens:
            dial_hits = sum(
                1 for p in products if product_matches_color_tokens(p, color_tokens)
            )
            if dial_hits == 0 and "encontrei" in reply.casefold():
                issues.append("presented_products_miss_dial_color")
        if goldish:
            gold_hits = sum(
                1 for p in products if product_matches_case_finish_tokens(p, case_tokens)
            )
            denied_gold = any(
                phrase in reply.casefold()
                for phrase in (
                    "nenhum veio confirmado em dourado",
                    "nenhum confirmado em dourado",
                    "sem dourado",
                )
            )
            if gold_hits and denied_gold:
                issues.append("false_negative_gold_case")
            if gold_hits == 0 and not denied_gold and len(products) >= 2:
                if "dourado" in _fold(text) or "gold" in _fold(text):
                    issues.append("listed_options_ignore_requested_gold")

    # 3) Persona / channel compliance — never break Crono character.
    if _PERSONA_LEAK_RE.search(reply):
        issues.append("persona_identity_leak")

    if not issues:
        return ComplianceReport(
            triggered=True,
            mode="enforce",
            verdict=ComplianceVerdict(
                pass_check=True,
                score=100,
                summary="Outbound compliance ok.",
            ),
            applied=False,
        )

    score = max(0, 100 - 25 * len(issues))
    return ComplianceReport(
        triggered=True,
        mode="enforce",
        verdict=ComplianceVerdict(
            pass_check=False,
            score=score,
            issues=issues,
            summary="; ".join(issues),
        ),
        applied=False,
    )


def apply_outbound_compliance(
    *,
    incoming: IncomingMessage | None,
    result: AgentResult,
    interpretation: SalesInterpretation | None = None,
) -> tuple[AgentResult, ComplianceReport]:
    """Attach report; rewrite dead links; re-rank preference shortlists."""
    report = check_outbound_compliance(
        incoming=incoming,
        result=result,
        interpretation=interpretation,
    )
    fixed = result.model_copy(deep=True)
    fixed.response_metadata = dict(fixed.response_metadata or {})
    fixed.response_metadata["outbound_compliance"] = report.model_dump(mode="json")

    verdict = report.verdict
    if verdict is None or verdict.pass_check:
        return fixed, report

    issues = set(verdict.issues)
    if issues & {"dead_product_url_in_reply", "photo_fallback_dead_link"}:
        name = None
        products = _presented_products(fixed)
        if products:
            name = str(products[0].get("name") or "").strip() or None
        label = name or "esse modelo"
        fixed.reply_text = (
            f"Não consegui enviar a foto de {label} agora e o link da vitrine "
            "desse item está inconsistente. Posso te mostrar outra opção da "
            "lista ou buscar de novo no catálogo?"
        )
        fixed.safety_reason = "product_media_dead_link"
        report = report.model_copy(update={"applied": True})
        fixed.response_metadata["outbound_compliance"] = report.model_dump(mode="json")

    preference_issues = issues & {
        "listed_options_ignore_requested_gold",
        "false_negative_gold_case",
        "presented_products_miss_dial_color",
    }
    if preference_issues and interpretation is not None:
        fixed, researched = _reresearch_preference_shortlist(
            result=fixed,
            interpretation=interpretation,
            issues=preference_issues,
        )
        if researched:
            report = report.model_copy(
                update={"applied": True, "reresearch_applied": True}
            )
            fixed.response_metadata = dict(fixed.response_metadata or {})
            fixed.response_metadata["outbound_compliance"] = report.model_dump(
                mode="json"
            )
            return fixed, report

    if "listed_options_ignore_requested_gold" in issues:
        if "dourado" not in (fixed.reply_text or "").casefold():
            fixed.reply_text = (
                (fixed.reply_text or "").rstrip()
                + "\n\nObs.: essas opções não vieram confirmadas em dourado; "
                "se quiser, busco só as combinações dourado + visor preto."
            )
            report = report.model_copy(update={"applied": True})
            fixed.response_metadata["outbound_compliance"] = report.model_dump(
                mode="json"
            )

    if "persona_identity_leak" in issues:
        fixed.reply_text = (
            "Sou o Crono da New Store Relógios. Em que posso te ajudar "
            "com relógios?"
        )
        fixed.safety_reason = "persona_compliance_rewrite"
        report = report.model_copy(update={"applied": True})
        fixed.response_metadata["outbound_compliance"] = report.model_dump(mode="json")

    return fixed, report
