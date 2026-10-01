"""Story / Vision / unviewable-media routes for the door."""

from __future__ import annotations

from app.configuration.runtime import message as operator_message

from typing import Any

from app.models import AgentResult, IncomingMessage


def _door():
    import app.agents.door as door_mod

    return door_mod


def unresolved_publication_reply(message: IncomingMessage) -> AgentResult | None:
    from app.channels.instagram_media_reference import has_unresolved_instagram_publication

    if not has_unresolved_instagram_publication(message):
        return None
    return AgentResult(
        reply_text="Recebi o link do Instagram, mas não consegui acessar a imagem ou o vídeo dessa publicação. "
                   "Pode enviar um print do relógio ou informar a marca e a referência?",
        intent="commerce",
        safety_reason="instagram_media_unviewable",
        response_metadata={"response_source": "instagram_unresolved_publication", "image_evidence_guard": True},
    )


async def try_media_routes(
    message: IncomingMessage,
    commerce_state: Any,
) -> AgentResult | None:
    door = _door()
    from app.stories.story_followup import active_story_reference, unresolved_story_followup
    from app.sales.ready_delivery import enrich_story_ready_delivery

    followup = unresolved_story_followup(message, commerce_state)
    if followup is not None:
        return await enrich_story_ready_delivery(message, followup)
    story_photo_ref = (
        active_story_reference(message, commerce_state, allow_image=True)
        if message.image_url else None
    )
    unresolved = unresolved_publication_reply(message)
    if unresolved is not None:
        return unresolved
    from app.stories.instagram_story_intent import story_requires_text_first

    if story_requires_text_first(message):
        # Do not let Story thumbnails fall through to generic image search.
        return None
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
                    story_agent = await enrich_story_ready_delivery(message, story_agent)
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
        story_turn = False
        try:
            from app.stories.instagram_story_intent import (
                should_route_story_question as _story_q,
            )

            story_turn = bool(_story_q(message))
        except Exception:
            story_turn = False
        if not story_turn:
            # Infra failure on the story import must not swallow a normal photo.
            pass
        else:
            skip_generic_image = True
            return door._annotate_agent_result(
                AgentResult(
                    reply_text=(
                        operator_message('agents.door_media.try_media_routes.8725e9c344')
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

    if skip_generic_image or (message.attachment_type or "").lower() == "video":
        return AgentResult(
            reply_text="Não consegui identificar com segurança o relógio dessa mídia. "
                       "Pode enviar um print nítido do mostrador ou a referência do modelo?",
            intent="commerce",
            safety_reason="instagram_media_unviewable",
            response_metadata={"response_source": "unresolved_media", "image_evidence_guard": True},
        )

    if door.image_search_eligible(message):
        from app.llm.llm_call_policy import build_llm_call_budget
        from app.ops.runtime_context import get_current_turn

        runtime = get_current_turn()
        if runtime is not None:
            image_budget = build_llm_call_budget(
                execution_path="complex",
                risk_signals=["image"],
            )
            runtime.promote_budget(int(image_budget.get("max_calls") or 0))
            runtime.execution_path = "complex"
        if story_photo_ref is not None:
            image_result = await door.handle_image_product_search(
                message,
                story_reference=story_photo_ref,
            )
        else:
            image_result = await door.handle_image_product_search(message)
        if image_result is not None:
            if image_result.response_metadata.get("support_document") or image_result.safety_reason == "image_not_watch":
                return image_result
            image_result.response_metadata['image_evidence_guard'] = True
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
                    reply_text=UNVIEWABLE_MEDIA_GUIDE_REPLY(),
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
                    reply_text=PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY(),
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
