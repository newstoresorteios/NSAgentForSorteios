"""Resolve catalog SKU references and inbound-vision early exits.

Look up patched names on ``app.sales_agent`` at call time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.commerce.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _sales():
    import app.sales_agent as sales_mod

    return sales_mod


@dataclass
class CatalogReferenceResolution:
    interpretation: SalesInterpretation | None
    resolved_product: Any
    resolved_by: str
    early_result: AgentResult | None = None


async def resolve_catalog_reference(
    *,
    message: IncomingMessage,
    interpretation: SalesInterpretation | None,
    plan: dict[str, Any],
    state: CommerceConversationState,
) -> CatalogReferenceResolution:
    """Bind or drop the contextual SKU; return an early reply when vision/list wins."""
    sales = _sales()
    resolved_product = None
    resolved_by = "none"
    if interpretation is None:
        return CatalogReferenceResolution(
            interpretation=None,
            resolved_product=None,
            resolved_by="none",
        )

    sales.log_purchase_progress("reference_resolution", "start")
    resolved_product, resolved_by = sales.resolve_commerce_reference(
        interpretation, state
    )
    sales.log_purchase_progress(
        "reference_resolution",
        "success" if resolved_product is not None else "blocked",
        None if resolved_product is not None else "reference_not_resolved",
    )
    print("[sales.reference]", {
        "type": interpretation.reference_type,
        "position": interpretation.reference_position,
        "resolved": resolved_product is not None,
        "resolved_by": resolved_by,
    })
    # "Quero ver um Seiko" is a fresh browse — never treat prior shortlist as
    # the target SKU (that skipped qualification and hammered Tray get_product).
    if (
        resolved_product is not None
        and interpretation.reference_type
        in {
            "previous_recommendation",
            "last_presented_product",
            "current_product",
        }
        and sales.is_open_catalog_browse_request(message.text, interpretation)
    ):
        print(
            "[sales.reference.ignore_stale_browse]",
            {
                "reference_type": interpretation.reference_type,
                "product_id": resolved_product.product_id,
                "brand": interpretation.subject.brand,
            },
        )
        resolved_product = None
        resolved_by = "none"
        interpretation = interpretation.model_copy(
            update={"reference_type": None, "reference_position": None}
        )
    if resolved_product is not None:
        from app.catalog.product_retrieval import required_model_tokens
        from app.sales.purchase_selection import (
            is_bare_purchase_closing,
            parse_list_position_selection,
        )

        purchase_close = bool(
            parse_list_position_selection(message.text)
            or is_bare_purchase_closing(message.text)
            or interpretation.reference_position is not None
            or interpretation.purchase_action
        )
        if sales._specific_product_lock(interpretation) and not purchase_close:
            tokens = required_model_tokens(interpretation.subject.model)
            hay = " ".join(
                part
                for part in (
                    resolved_product.name,
                    resolved_product.brand,
                    resolved_product.reference,
                )
                if part
            ).casefold()
            if tokens and not all(token in hay for token in tokens):
                print(
                    "[sales.reference.ignore_model_mismatch]",
                    {
                        "product_id": resolved_product.product_id,
                        "wanted_model": interpretation.subject.model,
                        "resolved_name": resolved_product.name,
                    },
                )
                resolved_product = None
                resolved_by = "none"
                interpretation = interpretation.model_copy(
                    update={"reference_type": None, "reference_position": None}
                )
    if resolved_product is not None:
        from app.sales.purchase_selection import (
            is_bare_purchase_closing,
            parse_list_position_selection,
        )
        from app.sales.tray_refresh import should_drop_contextual_resolve

        purchase_close = bool(
            parse_list_position_selection(message.text)
            or is_bare_purchase_closing(message.text)
            or interpretation.reference_position is not None
            or interpretation.purchase_action
        )
        if should_drop_contextual_resolve(
            interpretation=interpretation,
            message_text=message.text,
            purchase_close=purchase_close,
        ):
            print(
                "[sales.reference.ignore_constraint_change]",
                {
                    "product_id": resolved_product.product_id,
                    "color": interpretation.preferences.color,
                    "model": interpretation.subject.model,
                },
            )
            resolved_product = None
            resolved_by = "none"
            interpretation = interpretation.model_copy(
                update={"reference_type": None, "reference_position": None}
            )
    # Inbound photo must re-identify — never answer price from a stale
    # Kingfisher/sibling left in active/presented context.
    from app.catalog.vision.image_product_id import (
        handle_image_product_search,
        image_search_eligible,
    )
    from app.channels.brevo_instagram_media import (
        PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY,
        UNVIEWABLE_MEDIA_GUIDE_REPLY,
        is_bare_price_request,
        is_brevo_unviewable_media_text,
        should_guide_instagram_price_without_media,
    )
    from app.commerce.commerce_router import is_deictic_product_price_request

    vague_refs = {
        None,
        "none",
        "current_product",
        "last_presented_product",
        "previous_recommendation",
    }
    has_inbound_image = bool((message.image_url or "").strip())
    if is_brevo_unviewable_media_text(message.text):
        return CatalogReferenceResolution(
            interpretation=interpretation,
            resolved_product=resolved_product,
            resolved_by=resolved_by,
            early_result=sales._mark_sales_result(
                AgentResult(
                    reply_text=UNVIEWABLE_MEDIA_GUIDE_REPLY,
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="instagram_media_unviewable",
                    response_metadata={"domain": "commerce"},
                ),
                interpretation=interpretation,
                goal=interpretation.goal,
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason="brevo_instagram_media_unviewable",
            ),
        )
    # Instagram Story / unsupported IG media never arrives with image_url via
    # Brevo. Bare "valor" after that must not invent a SKU or ask vaguely.
    if (
        not has_inbound_image
        and should_guide_instagram_price_without_media(message)
        and interpretation.reference_type in vague_refs
        and interpretation.goal in {"inspect", "find", "discover", "recommend"}
        and resolved_product is None
        and state.active_product is None
    ):
        print("[sales.instagram.price_without_media]", {
            "bare_price": is_bare_price_request(message.text),
            "channel": message.channel,
        })
        return CatalogReferenceResolution(
            interpretation=interpretation,
            resolved_product=resolved_product,
            resolved_by=resolved_by,
            early_result=sales._mark_sales_result(
                AgentResult(
                    reply_text=PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY,
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="instagram_media_unviewable",
                    response_metadata={"domain": "commerce"},
                ),
                interpretation=interpretation,
                goal=interpretation.goal,
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason="instagram_price_without_media",
            ),
        )
    # Brevo often splits photo+caption: text "qual o preço desse?" arrives
    # without image_url and would price the previous SKU (CW Rosa → Beaubleu).
    if (
        not has_inbound_image
        and is_deictic_product_price_request(message.text)
        and interpretation.reference_type in vague_refs
        and interpretation.goal in {"inspect", "find"}
    ):
        print("[sales.reference.ignore_stale_deictic]", {
            "had_resolved": resolved_product is not None,
            "reference_type": interpretation.reference_type,
            "active_product_id": (
                state.active_product.product_id if state.active_product else None
            ),
        })
        resolved_product = None
        resolved_by = "none"
        if state.active_product is not None:
            state.active_product = None
        # Caption-only fragment before the photo lands — wait for the image
        # instead of quoting the previous watch.
        if not (
            state.product_resolution_state == "plausible_matches"
            and state.last_presented_products
        ):
            return CatalogReferenceResolution(
                interpretation=interpretation,
                resolved_product=resolved_product,
                resolved_by=resolved_by,
                early_result=sales._mark_sales_result(
                    AgentResult(
                        reply_text=(
                            "Recebi sua pergunta de preço. Se for o relógio da foto, "
                            "me envia a imagem (ou a marca e o modelo) que eu confirmo "
                            "no catálogo e te passo o valor certinho."
                        ),
                        intent="commerce",
                        handoff_required=False,
                        safety_reason="product_context_missing",
                        response_metadata={
                            "domain": "commerce",
                            "clear_active_product": True,
                        },
                    ),
                    interpretation=interpretation,
                    goal=interpretation.goal,
                    response_source="deterministic_fallback",
                    used_openai_responder=False,
                    used_tray=False,
                    fallback_reason="deictic_price_without_image",
                ),
            )
    if (
        image_search_eligible(message)
        and interpretation.reference_type in vague_refs
        and interpretation.goal in {
            "find",
            "inspect",
            "recommend",
            "discover",
            "compare",
        }
    ):
        image_result = await handle_image_product_search(message)
        if image_result is not None:
            return CatalogReferenceResolution(
                interpretation=interpretation,
                resolved_product=resolved_product,
                resolved_by=resolved_by,
                early_result=sales._mark_sales_result(
                    image_result,
                    interpretation=interpretation,
                    goal="find",
                    response_source=image_result.response_metadata.get(
                        "response_source",
                        "image_vision",
                    ),
                    used_openai_responder=bool(
                        image_result.response_metadata.get("used_openai_responder")
                    ),
                    used_tray=bool(
                        image_result.response_metadata.get("used_tray")
                        or (image_result.commercial_data or {}).get("products")
                    ),
                    fallback_reason=image_result.safety_reason,
                ),
            )
    # Soft nearby siblings are not a confirmed product for "qual o preço?".
    # Follow-ups about the listed models (pronta entrega, todos, desses)
    # must stay on that list — never dump a stale photo-match catalog.
    if (
        not interpretation.image_request
        and not sales.is_outbound_catalog_image_request(message.text)
        and sales.is_listed_catalog_follow_up(message.text)
        and state.last_presented_products
        and not (message.image_url or "").strip()
    ):
        inspect_result = await sales._inspect_listed_products(state)
        final = await sales._sales_response_with_openai(
            message,
            plan,
            inspect_result,
            interpretation,
            state=state,
        )
        if final:
            return CatalogReferenceResolution(
                interpretation=interpretation,
                resolved_product=resolved_product,
                resolved_by=resolved_by,
                early_result=final,
            )
        return CatalogReferenceResolution(
            interpretation=interpretation,
            resolved_product=resolved_product,
            resolved_by=resolved_by,
            early_result=sales._mark_sales_result(
                inspect_result,
                interpretation=interpretation,
                goal=interpretation.goal,
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=True,
                fallback_reason=inspect_result.safety_reason,
            ),
        )
    if (
        not interpretation.image_request
        and not sales.is_outbound_catalog_image_request(message.text)
        and not sales.is_listed_catalog_follow_up(message.text)
        and state.product_resolution_state == "plausible_matches"
        and interpretation.goal == "inspect"
        and interpretation.reference_type in vague_refs
        and state.last_presented_products
    ):
        from app.commerce.commerce_router import _product_lines

        numbered = [
            f"{position}. {line}"
            for position, line in enumerate(
                _product_lines(
                    [
                        item.model_dump(mode="json")
                        for item in state.last_presented_products[:3]
                    ],
                    compact=True,
                ),
                start=1,
            )
        ]
        return CatalogReferenceResolution(
            interpretation=interpretation,
            resolved_product=resolved_product,
            resolved_by=resolved_by,
            early_result=sales._mark_sales_result(
                AgentResult(
                    reply_text=(
                        "Ainda não confirmei o modelo exato. Destes que listei, "
                        "qual você quer o preço?\n"
                        + "\n".join(numbered)
                    ),
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="exact_product_ambiguous_brand",
                    commercial_data={
                        "products": [
                            item.model_dump(mode="json")
                            for item in state.last_presented_products[:3]
                        ],
                        "match_status": "ambiguous",
                    },
                    response_metadata={
                        "presented_products": True,
                        "product_resolution_state": "plausible_matches",
                        "clear_active_product": True,
                        "domain": "commerce",
                    },
                ),
                interpretation=interpretation,
                goal=interpretation.goal,
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason="plausible_matches_price_blocked",
            ),
        )
    return CatalogReferenceResolution(
        interpretation=interpretation,
        resolved_product=resolved_product,
        resolved_by=resolved_by,
    )
