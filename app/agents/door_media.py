"""Story / Vision / unviewable-media routes for the door."""

from __future__ import annotations

from typing import Any

from app.models import AgentResult, IncomingMessage


def _door():
    import app.agents.door as door_mod

    return door_mod


async def try_media_routes(
    message: IncomingMessage,
    commerce_state: Any,
) -> AgentResult | None:
    door = _door()
    skip_generic_image = False
    try:
        from app.stories.instagram_story_intent import should_route_story_question
        from app.stories.instagram_story_service import (
            resolve_story_product_question,
            story_result_to_agent_result,
        )

        if should_route_story_question(message):
            skip_generic_image = True
            story_resolution = await resolve_story_product_question(
                incoming=message,
                execute_tool=door.execute_tool,
            )
            if story_resolution is not None:
                story_agent = story_result_to_agent_result(
                    story_resolution,
                    incoming=message,
                )
                if story_agent is not None:
                    return door._annotate_agent_result(
                        story_agent,
                        domain="commerce",
                        goal="inspect",
                        response_source="instagram_story",
                        used_openai_interpreter=False,
                        used_openai_responder=False,
                        used_tray=bool(story_resolution.product_payload),
                        fallback_reason=story_resolution.failure_reason,
                    )
    except Exception as exc:  # noqa: BLE001
        print(
            "[instagram.story.route.error]",
            {"error_type": type(exc).__name__, "error": str(exc)[:240]},
        )
        skip_generic_image = True
        from app.stories.instagram_story_intent import (
            should_route_story_question as _story_q,
        )

        if _story_q(message):
            return door._annotate_agent_result(
                AgentResult(
                    reply_text=(
                        "Identifiquei o Story, mas não consegui confirmar o valor "
                        "agora. Posso tentar de novo em instantes."
                    ),
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="story_route_error",
                    response_metadata={"domain": "commerce", "instagram_story": True},
                ),
                domain="commerce",
                goal="inspect",
                response_source="instagram_story",
                used_openai_interpreter=False,
                used_openai_responder=False,
                used_tray=False,
                fallback_reason="story_route_error",
            )

    if not skip_generic_image and door.image_search_eligible(message):
        image_result = await door.handle_image_product_search(message)
        if image_result is not None:
            return door._annotate_agent_result(
                image_result,
                domain="commerce",
                goal="find",
                response_source=(
                    "technical_fallback"
                    if image_result.safety_reason
                    in {
                        "image_identify_failed",
                        "tray_adapter_unavailable",
                        "product_match_failed",
                    }
                    else image_result.response_metadata.get(
                        "response_source",
                        "image_vision",
                    )
                ),
                used_openai_interpreter=False,
                used_openai_responder=bool(
                    image_result.response_metadata.get("used_openai_responder")
                ),
                used_tray=bool(image_result.response_metadata.get("used_tray")),
                fallback_reason=image_result.safety_reason,
            )

    try:
        from app.channels.brevo_instagram_media import (
            PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY,
            UNVIEWABLE_MEDIA_GUIDE_REPLY,
            is_brevo_unviewable_media_text,
            should_guide_instagram_price_without_media,
        )

        if is_brevo_unviewable_media_text(message.text):
            return door._annotate_agent_result(
                AgentResult(
                    reply_text=UNVIEWABLE_MEDIA_GUIDE_REPLY,
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="instagram_media_unviewable",
                ),
                domain="commerce",
                goal="inspect",
                response_source="deterministic_fallback",
                used_openai_interpreter=False,
                used_openai_responder=False,
                used_tray=False,
                fallback_reason="brevo_instagram_media_unviewable",
            )
        if (
            should_guide_instagram_price_without_media(message)
            and getattr(commerce_state, "active_product", None) is None
        ):
            return door._annotate_agent_result(
                AgentResult(
                    reply_text=PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY,
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="instagram_media_unviewable",
                ),
                domain="commerce",
                goal="inspect",
                response_source="deterministic_fallback",
                used_openai_interpreter=False,
                used_openai_responder=False,
                used_tray=False,
                fallback_reason="instagram_price_without_media",
            )
    except Exception as exc:  # noqa: BLE001
        print("[brevo.instagram_media.guide.error]", {"error_type": type(exc).__name__})
    return None
