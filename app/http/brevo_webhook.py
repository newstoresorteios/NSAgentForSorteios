"""Brevo WhatsApp / Conversations webhook."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.channels.brevo_client import send_brevo_reply as _send_brevo_reply
from app.channels.inbound_coalesce import (
    attach_recent_image_for_followup,
    is_caption_echo_of_recent_image as _is_caption_echo_of_recent_image,
)
from app.channels.webhook_parser import (
    inbound_skip_reason,
    parse_brevo_conversations_payload,
    select_effective_inbound_message,
    selected_message_info,
    webhook_event_skip_reason,
)
from app.config import get_allowed_channels, get_settings as _get_settings
from app.db import (
    claim_inbound_message as _claim_inbound_message,
    has_successful_agent_response as _has_successful_agent_response,
    inbound_already_completed as _inbound_already_completed,
    inbound_message_exists as _inbound_message_exists,
    insert_agent_response as _insert_agent_response,
    insert_inbound_message as _insert_inbound_message,
    is_latest_inbound_message as _is_latest_inbound_message,
)
from app.http.bindings import resolve
from app.http.payload import read_request_payload, skip_webhook_event, webhook_event_name
from app.identity.repository import find_customer_profile_by_phone as _find_customer_profile_by_phone
from app.learning.remarketing import sync_remarketing_interaction as _sync_remarketing_interaction
from app.message_pipeline import process_incoming_message as _process_incoming_message
from app.models import AgentResult
from app.ops.conversation_lock import (
    ConversationLockUnavailable,
    acquire_conversation_lock as _acquire_conversation_lock,
    conversation_lock_key,
    release_conversation_lock as _release_conversation_lock,
)
from app.ops.handoff_service import apply_integration_failure_handoff, handoff_provider_payload
from app.ops.observability import log_event, log_exception, redact_text, summarize_webhook_payload
from app.ops.runtime_context import get_current_turn, runtime_stage
from app.security import verify_brevo_webhook

router = APIRouter(tags=["webhooks-brevo"])


async def handle_brevo_conversations_webhook(request: Request) -> JSONResponse:

    claim_inbound_message = resolve("claim_inbound_message", _claim_inbound_message)
    inbound_message_exists = resolve("inbound_message_exists", _inbound_message_exists)
    inbound_already_completed = resolve("inbound_already_completed", _inbound_already_completed)
    has_successful_agent_response = resolve("has_successful_agent_response", _has_successful_agent_response)
    is_latest_inbound_message = resolve("is_latest_inbound_message", _is_latest_inbound_message)
    insert_agent_response = resolve("insert_agent_response", _insert_agent_response)
    find_customer_profile_by_phone = resolve("find_customer_profile_by_phone", _find_customer_profile_by_phone)
    process_incoming_message = resolve("process_incoming_message", _process_incoming_message)
    send_brevo_reply = resolve("send_brevo_reply", _send_brevo_reply)
    get_settings = resolve("get_settings", _get_settings)
    acquire_conversation_lock = resolve("acquire_conversation_lock", _acquire_conversation_lock)
    release_conversation_lock = resolve("release_conversation_lock", _release_conversation_lock)
    is_caption_echo_of_recent_image = resolve("is_caption_echo_of_recent_image", _is_caption_echo_of_recent_image)
    insert_inbound_message = resolve("insert_inbound_message", _insert_inbound_message)
    sync_remarketing_interaction = resolve(
        "sync_remarketing_interaction", _sync_remarketing_interaction
    )
    try:
        payload = await read_request_payload(request)
    except HTTPException:
        log_event(
            "brevo.webhook.routing",
            {
                "event_name": None,
                "should_process": False,
                "reason": "invalid_payload",
            },
        )
        log_event(
            "brevo.webhook.skipped",
            {"event_name": None, "reason": "invalid_payload"},
        )
        raise

    event_name = webhook_event_name(payload)
    log_event(
        "brevo.webhook.received",
        {
            "content_type": request.headers.get("content-type"),
            "has_body": True,
            **summarize_webhook_payload(payload if isinstance(payload, dict) else {}),
        },
    )

    event_skip_reason = webhook_event_skip_reason(payload)
    if event_skip_reason:
        return skip_webhook_event(
            event_name=event_name,
            reason=event_skip_reason,
        )

    try:
        incoming = parse_brevo_conversations_payload(payload)
    except Exception as exc:
        log_exception(
            "brevo.webhook.parse_failed",
            exc,
            {
                "event_name": payload.get("eventName") if isinstance(payload, dict) else None,
                "parsed": False,
            },
        )
        log_event(
            "brevo.webhook.parsed",
            {
                "parsed": False,
                "event_name": payload.get("eventName") if isinstance(payload, dict) else None,
                "channel": "unknown",
                "sender_key_present": False,
                "visitor_id_present": False,
                "source_conversation_ref_present": False,
                "message_id_present": False,
                "conversation_id_present": False,
                "sender_phone_present": False,
                "text_present": False,
                "input_modality": None,
                "attachment_type": None,
                "direction": None,
            },
        )
        return skip_webhook_event(
            event_name=event_name,
            reason="invalid_payload",
            error_type=type(exc).__name__,
        )

    runtime = get_current_turn()
    if runtime is not None:
        runtime.channel = incoming.channel
        runtime.conversation_key = (
            incoming.conversation_id
            or incoming.sender_key
            or incoming.visitor_id
            or incoming.sender_phone
            or "unresolved"
        )

    log_event(
        "brevo.webhook.parsed",
        {
            "parsed": True,
            "event_name": incoming.event_type,
            "channel": incoming.channel,
            "sender_key_present": bool(incoming.sender_key),
            "visitor_id_present": bool(incoming.visitor_id),
            "source_conversation_ref_present": bool(incoming.source_conversation_ref),
            "message_id_present": bool(incoming.message_id),
            "conversation_id_present": bool(incoming.conversation_id),
            "sender_phone_present": bool(incoming.sender_phone),
            "sender_name_present": bool(incoming.sender_name),
            "text_present": bool(incoming.text),
            "text_chars": len(incoming.text or ""),
            "text_preview": redact_text(incoming.text, max_chars=200),
            "input_modality": incoming.input_modality,
            "attachment_type": incoming.attachment_type,
            "image_url_present": bool((incoming.image_url or "").strip()),
            "audio_url_present": bool(incoming.audio_url),
            "direction": selected_message_info(payload).get("role"),
        },
    )

    selected = select_effective_inbound_message(payload)
    selection_info = selected_message_info(payload, selected)
    log_event(
        "brevo.webhook.selected_message",
        {
            "message_id_present": bool(incoming.message_id),
            "role": selection_info.get("role"),
            "timestamp_present": selection_info.get("timestamp_present"),
            "text_length": len(incoming.text or ""),
            "text_hash": hashlib.sha256((incoming.text or "").encode("utf-8")).hexdigest()[:12],
            "ordering_fallback": selection_info.get("ordering_fallback"),
            "channel": incoming.channel,
            "event_name": incoming.event_type,
            "input_modality": incoming.input_modality,
            "attachment_type": incoming.attachment_type,
        },
    )

    settings = get_settings()
    allowed_channels = get_allowed_channels(settings)

    skip_reason = inbound_skip_reason(payload)
    if skip_reason:
        # Brevo labels Instagram Story / unsupported IG media as an "agent"
        # placeholder without attachment URL. Do not treat that as human takeover,
        # and guide the visitor to resend a normal photo.
        from app.channels.brevo_instagram_media import (
            UNVIEWABLE_MEDIA_GUIDE_REPLY,
            is_brevo_unviewable_media_text,
        )

        if (
            skip_reason in {"agent_message", "outbound_message"}
            and is_brevo_unviewable_media_text(incoming.text)
            and (incoming.channel or "").lower() == "instagram"
            and "instagram" in allowed_channels
            and bool(getattr(settings, "brevo_social_channels_enabled", True))
        ):
            log_event(
                "brevo.instagram_media_unviewable",
                {
                    "event_name": event_name,
                    "channel": incoming.channel,
                    "conversation_id_present": bool(incoming.conversation_id),
                    "visitor_id_present": bool(incoming.visitor_id),
                    "message_id_present": bool(incoming.message_id),
                    "skip_reason": skip_reason,
                },
            )
            if incoming.message_id and inbound_message_exists(
                incoming.provider, incoming.message_id
            ):
                return skip_webhook_event(
                    event_name=event_name,
                    reason="instagram_media_unviewable_duplicate",
                )
            try:
                claimed, inbound_id = claim_inbound_message(incoming.model_dump())
            except Exception as exc:
                log_exception(
                    "brevo.webhook.unviewable_inbound_failed",
                    exc,
                    {"event_name": event_name},
                )
                return skip_webhook_event(
                    event_name=event_name,
                    reason="instagram_media_unviewable",
                )
            if not claimed:
                return skip_webhook_event(
                    event_name=event_name,
                    reason="instagram_media_unviewable_duplicate",
                )
            guide = AgentResult(
                reply_text=UNVIEWABLE_MEDIA_GUIDE_REPLY,
                intent="commerce",
                handoff_required=False,
                safety_reason="instagram_media_unviewable",
                response_metadata={
                    "domain": "commerce",
                    "response_source": "deterministic_fallback",
                    "fallback_reason": "brevo_instagram_media_unviewable",
                },
            )
            send_result = await send_brevo_reply(incoming, guide)
            try:
                insert_agent_response(
                    {
                        "inbound_id": inbound_id,
                        "channel": incoming.channel,
                        "sender_key": incoming.sender_key,
                        "sender_phone": incoming.sender_phone,
                        "reply_text": guide.reply_text,
                        "intent": guide.intent,
                        "handoff_required": guide.handoff_required,
                        "safety_reason": guide.safety_reason,
                        "provider_send_ok": bool(send_result.ok),
                        "provider_response": send_result.model_dump(),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                log_exception(
                    "brevo.webhook.unviewable_response_persist_failed",
                    exc,
                    {"inbound_id": inbound_id},
                )
            log_event(
                "brevo.webhook.unviewable_media_guided",
                {
                    "inbound_id": inbound_id,
                    "send_ok": bool(send_result.ok),
                    "channel": incoming.channel,
                },
            )
            return JSONResponse(
                {
                    "ok": True,
                    "skipped": False,
                    "reason": "instagram_media_unviewable_guided",
                    "inbound_id": inbound_id,
                }
            )

        if skip_reason in {"agent_message", "outbound_message"}:
            try:
                from app.ops.human_takeover import touch_human_activity

                touch_human_activity(incoming)
            except Exception as exc:  # noqa: BLE001
                log_exception(
                    "brevo.webhook.human_takeover_touch_failed",
                    exc,
                    {"skip_reason": skip_reason},
                )
        return skip_webhook_event(
            event_name=event_name,
            reason=skip_reason,
        )

    if incoming.channel not in allowed_channels:
        return skip_webhook_event(
            event_name=event_name,
            reason="channel_not_allowed",
        )
    if incoming.channel in {"instagram", "facebook"} and not settings.brevo_social_channels_enabled:
        return skip_webhook_event(
            event_name=event_name,
            reason="social_channels_disabled",
        )

    if not any((incoming.sender_key, incoming.visitor_id, incoming.conversation_id)):
        return skip_webhook_event(
            event_name=event_name,
            reason="missing_sender_identity",
        )

    if (
        not incoming.text.strip()
        and not incoming.audio_url
        and not (incoming.image_url or "").strip()
    ):
        return skip_webhook_event(
            event_name=event_name,
            reason="no_text",
        )

    # FASE 2: optional durable enqueue — HTTP 200 before agent turn.
    if bool(getattr(settings, "agent_async_ingress_enabled", False)):
        from app.ingress.inbox import enqueue_inbound

        created, inbox_id = enqueue_inbound(
            provider=incoming.provider or "brevo",
            channel=incoming.channel,
            message_id=incoming.message_id,
            conversation_key=incoming.conversation_id or incoming.sender_key,
            visitor_id=incoming.visitor_id,
            sender_key=incoming.sender_key,
            event_name=event_name,
            payload={
                "normalized": incoming.model_dump(mode="json"),
                "raw": incoming.raw if isinstance(incoming.raw, dict) else {},
            },
        )
        return JSONResponse(
            {
                "ok": True,
                "queued": True,
                "created": created,
                "inbox_id": inbox_id,
                "async_ingress": True,
            }
        )

    # Cheap duplicate check before waiting on the conversation lock. Brevo often
    # redelivers the same fragment while Vision/catalog still holds the lock.
    if incoming.message_id and inbound_already_completed(
        incoming.provider, incoming.message_id
    ):
        return skip_webhook_event(
            event_name=event_name,
            reason="duplicate_message",
        )

    if getattr(settings, "agent_conversation_lock_enabled", True):
        lock_key = conversation_lock_key(
            conversation_id=incoming.conversation_id,
            sender_key=incoming.sender_key,
            sender_phone=incoming.sender_phone,
            visitor_id=incoming.visitor_id,
        )
        if lock_key:
            lock_timeout = float(
                getattr(
                    settings,
                    "agent_conversation_lock_timeout_seconds",
                    15.0,
                )
            )
            # Keep waits short: the in-flight turn owns the work. Do not
            # return 503 — Brevo retries and the next interpret can change
            # meaning (image follow-up → buy). Queue the same inbound instead.
            if (incoming.image_url or "").strip() or (
                (incoming.attachment_type or "").lower() == "image"
            ):
                lock_timeout = min(max(lock_timeout, 5.0), 12.0)
            from app.ingress.busy import (
                LOCK_DEFERRED_RETRY_SECONDS,
                enqueue_lock_deferred_inbound,
                inbox_drain_keys,
            )

            request.state.inbox_drain_keys = inbox_drain_keys(
                lock_key=lock_key,
                incoming=incoming,
            )
            try:
                with runtime_stage("conversation_lock_wait"):
                    request.state.conversation_lock_handle = (
                        await acquire_conversation_lock(
                            lock_key,
                            database_url=getattr(
                                settings,
                                "database_url",
                                "",
                            ),
                            timeout_seconds=lock_timeout,
                        )
                    )
            except ConversationLockUnavailable as exc:
                lock_error = str(exc)
                log_event(
                    "agent.lock.unavailable",
                    {
                        "error": lock_error,
                        "channel": incoming.channel,
                        "conversation_id_present": bool(
                            incoming.conversation_id
                        ),
                        "sender_key_present": bool(incoming.sender_key),
                        "inbound_image": bool((incoming.image_url or "").strip()),
                        "lock_timeout_seconds": lock_timeout,
                    },
                )
                # Infra failure: keep a local-only lock so photo turns survive.
                if lock_error == "database_lock_unavailable":
                    try:
                        request.state.conversation_lock_handle = (
                            await acquire_conversation_lock(
                                lock_key,
                                database_url="",
                                timeout_seconds=min(lock_timeout, 5.0),
                            )
                        )
                        log_event(
                            "agent.lock.local_fallback",
                            {
                                "channel": incoming.channel,
                                "inbound_image": bool(
                                    (incoming.image_url or "").strip()
                                ),
                            },
                        )
                    except ConversationLockUnavailable:
                        created, inbox_id = enqueue_lock_deferred_inbound(
                            incoming,
                            event_name=event_name,
                            conversation_key=lock_key,
                        )
                        return JSONResponse(
                            {
                                "ok": True,
                                "queued": True,
                                "reason": "conversation_busy",
                                "inbox_id": inbox_id,
                                "created": created,
                            }
                        )
                else:
                    created, inbox_id = enqueue_lock_deferred_inbound(
                        incoming,
                        event_name=event_name,
                        conversation_key=lock_key,
                    )
                    try:
                        request.state.conversation_lock_handle = (
                            await acquire_conversation_lock(
                                lock_key,
                                database_url=getattr(
                                    settings,
                                    "database_url",
                                    "",
                                ),
                                timeout_seconds=LOCK_DEFERRED_RETRY_SECONDS,
                            )
                        )
                        log_event(
                            "agent.lock.deferred_retry_acquired",
                            {
                                "channel": incoming.channel,
                                "inbox_id": inbox_id,
                            },
                        )
                    except ConversationLockUnavailable:
                        return JSONResponse(
                            {
                                "ok": True,
                                "queued": True,
                                "reason": "conversation_busy",
                                "inbox_id": inbox_id,
                                "created": created,
                            }
                        )

    # Brevo often redelivers the caption as a second text-only webhook after the
    # photo+caption turn. Skip exact caption echoes so we don't reply twice.
    if not (incoming.image_url or "").strip() and is_caption_echo_of_recent_image(
        incoming
    ):
        return skip_webhook_event(
            event_name=event_name,
            reason="caption_echo",
        )

    incoming = attach_recent_image_for_followup(incoming)

    try:
        claimed, inbound_id = claim_inbound_message(incoming.model_dump())
    except Exception as exc:
        log_exception(
            "brevo.webhook.inbound_insert_failed",
            exc,
            {
                "event_type": incoming.event_type,
                "channel": incoming.channel,
                "sender_key_present": bool(incoming.sender_key),
                "visitor_id_present": bool(incoming.visitor_id),
                "conversation_id_present": bool(incoming.conversation_id),
                "sender_phone_present": bool(incoming.sender_phone),
                "message_id_present": bool(incoming.message_id),
                "input_modality": incoming.input_modality,
                "attachment_type": incoming.attachment_type,
            },
        )
        raise HTTPException(status_code=500, detail={"error": "inbound_insert_failed"}) from exc
    if not claimed:
        if inbound_id and has_successful_agent_response(inbound_id):
            return skip_webhook_event(
                event_name=event_name,
                reason="duplicate_message",
            )
        if not inbound_id:
            return skip_webhook_event(
                event_name=event_name,
                reason="duplicate_message",
            )

    # Central ChatBô: se um humano assumiu, grava inbound mas não responde.
    try:
        from app.ops.human_takeover import human_takeover_active

        if human_takeover_active(incoming):
            return skip_webhook_event(
                event_name=event_name,
                reason="human_takeover",
            )
    except Exception as exc:  # noqa: BLE001
        log_exception(
            "brevo.webhook.human_takeover_check_failed",
            exc,
            {"inbound_id": inbound_id},
        )

    if runtime is not None:
        runtime.inbound_id = inbound_id
    if isinstance(incoming.raw, dict):
        incoming.raw["inbound_id"] = inbound_id
    if incoming.sender_phone:
        customer_context = find_customer_profile_by_phone(incoming.sender_phone)
    else:
        customer_context = {
            "found": False,
            "channel": incoming.channel,
            "sender_key": incoming.sender_key,
            "display_name": incoming.sender_name,
        }
    log_event(
        "brevo.webhook.routing",
        {
            "event_name": event_name,
            "should_process": True,
            "reason": "inbound_message",
            "inbound_id": inbound_id,
            "customer_found": bool(customer_context.get("found")),
        },
    )
    log_event(
        "brevo.webhook.processing",
        {
            "channel": incoming.channel,
            "sender_key_present": bool(incoming.sender_key),
            "visitor_id_present": bool(incoming.visitor_id),
            "source_conversation_ref_present": bool(incoming.source_conversation_ref),
            "conversation_id_present": bool(incoming.conversation_id),
            "sender_phone_present": bool(incoming.sender_phone),
            "message_id_present": bool(incoming.message_id),
            "event_name": incoming.event_type,
            "input_modality": incoming.input_modality,
            "attachment_type": incoming.attachment_type,
            "text_preview": redact_text(incoming.text, max_chars=200),
            "inbound_id": inbound_id,
            "customer_found": bool(customer_context.get("found")),
        },
    )
    from app.ingress.outbox import get_accepted_outbound, result_from_outbox_row
    accepted = get_accepted_outbound(inbound_id)
    if accepted is not None:
        agent_result = result_from_outbox_row(accepted)
    else:
        agent_result = await process_incoming_message(incoming, customer_context)
        agent_result = apply_integration_failure_handoff(agent_result)

    log_event(
        "brevo.webhook.agent_result",
        {
            "intent": agent_result.intent,
            "handoff_required": agent_result.handoff_required,
            "safety_reason": agent_result.safety_reason,
                "response_metadata": agent_result.response_metadata,
            "reply_length": len(agent_result.reply_text or ""),
            "reply_preview": redact_text(agent_result.reply_text, max_chars=200),
            "channel": incoming.channel,
            "input_modality": incoming.input_modality,
            "attachment_type": incoming.attachment_type,
            "transcription_failed": incoming.transcription_failed,
            "response_source": (agent_result.response_metadata or {}).get(
                "response_source"
            ),
            "domain": (agent_result.response_metadata or {}).get("domain"),
            "goal": (agent_result.response_metadata or {}).get("goal"),
            "used_openai_interpreter": bool(
                (agent_result.response_metadata or {}).get("used_openai_interpreter")
            ),
            "used_openai_responder": bool(
                (agent_result.response_metadata or {}).get("used_openai_responder")
            ),
            "used_tray": bool((agent_result.response_metadata or {}).get("used_tray")),
        },
    )

    if not is_latest_inbound_message(
        inbound_id,
        incoming.conversation_id,
        incoming.sender_key,
        incoming.sender_phone,
    ):
        log_event(
            "brevo.webhook.skipped_reply",
            {"reason": "stale_inbound", "inbound_id": inbound_id},
        )
        send_result = None
        provider_send_ok = False
        provider_response = {"skipped": True, "reason": "stale_inbound"}
    elif (
        getattr(settings, "agent_send_idempotency_enabled", True)
        and has_successful_agent_response(inbound_id)
    ):
        log_event(
            "brevo.webhook.skipped_reply",
            {
                "reason": "already_sent",
                "inbound_id": inbound_id,
            },
        )
        send_result = None
        provider_send_ok = True
        provider_response = {"skipped": True, "reason": "already_sent"}
    else:
        from app.ingress.outbox import (
            enqueue_accepted_outbound,
            mark_outbox_failed,
            mark_outbox_sent,
        )

        outbox_id = enqueue_accepted_outbound(
            incoming=incoming,
            result=agent_result,
            inbound_id=inbound_id,
        )
        from app.ingress.outbox import dispatch_accepted_outbound

        send_result = None
        async def send_accepted(accepted_incoming, accepted_result):
            nonlocal send_result
            send_result = await send_brevo_reply(accepted_incoming, accepted_result)
            return send_result.model_dump()

        if outbox_id is not None:
            provider_response = await dispatch_accepted_outbound(outbox_id, send_accepted)
        else:
            provider_response = await send_accepted(incoming, agent_result)
        provider_send_ok = bool(provider_response.get("ok"))
        log_event("brevo.webhook.send_result", {
            "ok": provider_send_ok, "queued": bool(provider_response.get("queued")),
            "channel": incoming.channel, "inbound_id": inbound_id,
        })
    commerce_state = (agent_result.response_metadata or {}).get("commerce_state")
    decision_snapshot = (agent_result.response_metadata or {}).get(
        "decision_snapshot"
    )
    factual_validation = (agent_result.response_metadata or {}).get(
        "factual_validation"
    )
    quality_judge = (agent_result.response_metadata or {}).get("quality_judge")
    handoff_payload = handoff_provider_payload(agent_result)
    runtime_summary = None
    if runtime is not None:
        runtime.register_fallback(
            (agent_result.response_metadata or {}).get("fallback_reason")
            or agent_result.safety_reason
        )
        runtime_summary = runtime.safe_summary()
        agent_result.response_metadata["turn_runtime"] = runtime_summary
    if isinstance(provider_response, dict):
        agent_context: dict[str, object] = {}
        if isinstance(commerce_state, dict):
            agent_context["commerce_state"] = commerce_state
        if isinstance(decision_snapshot, dict):
            agent_context["decision_snapshot"] = decision_snapshot
        if isinstance(factual_validation, dict):
            agent_context["factual_validation"] = factual_validation
        if isinstance(quality_judge, dict):
            agent_context["quality_judge"] = quality_judge
        if isinstance(handoff_payload, dict):
            agent_context["handoff"] = handoff_payload
        if agent_context:
            provider_response["_agent_context"] = agent_context
    if isinstance(provider_response, dict) and isinstance(runtime_summary, dict):
        provider_response["_agent_runtime"] = runtime_summary

    try:
        response_id = insert_agent_response(
            {
                "inbound_id": inbound_id,
                "channel": incoming.channel,
                "sender_key": incoming.sender_key,
                "sender_phone": incoming.sender_phone,
                "reply_text": agent_result.reply_text,
                "intent": agent_result.intent,
                "handoff_required": agent_result.handoff_required,
                "safety_reason": agent_result.safety_reason,
                "response_metadata": agent_result.response_metadata,
                "provider_send_ok": provider_send_ok,
                "provider_response": provider_response,
            }
        )
        try:
            from app.learning.attendance_learning import (
                attach_response_id_to_pipeline_reviews,
            )

            attach_response_id_to_pipeline_reviews(
                inbound_id=inbound_id,
                response_id=response_id,
            )
        except Exception as exc:
            log_exception(
                "brevo.webhook.pipeline_review_bind_failed",
                exc,
                {"inbound_id": inbound_id},
            )
    except Exception as exc:
        log_exception(
            "brevo.webhook.response_insert_failed",
            exc,
            {
                "inbound_id": inbound_id,
                "channel": incoming.channel,
            },
        )
        if provider_send_ok:
            return JSONResponse(
                {
                    "ok": True,
                    "inbound_id": inbound_id,
                    "reply_dry_run": send_result.dry_run if send_result else False,
                    "reply_sent": True,
                    "handoff_required": agent_result.handoff_required,
                    "skipped_reply": False,
                    "persist_warning": "response_insert_failed",
                }
            )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "response_insert_failed",
                "message": "Falha ao registrar resposta do agente.",
            },
        ) from exc

    try:
        sync_remarketing_interaction(
            incoming,
            inbound_id=inbound_id,
            response_metadata=(
                agent_result.response_metadata
                if provider_send_ok
                else {}
            ),
            handoff_required=agent_result.handoff_required,
        )
    except Exception as exc:
        log_exception(
            "remarketing.sync.failed",
            exc,
            {
                "inbound_id": inbound_id,
                "channel": incoming.channel,
            },
        )

    if agent_result.handoff_required and provider_send_ok:
        try:
            from app.ops.handoff_queue import mark_conversa_for_human_handoff

            mark_conversa_for_human_handoff(
                incoming,
                reason=agent_result.safety_reason or "handoff_required",
            )
        except Exception as exc:
            log_exception(
                "handoff.queue.failed",
                exc,
                {
                    "inbound_id": inbound_id,
                    "channel": incoming.channel,
                },
            )

    return JSONResponse(
        {
            "ok": True,
            "inbound_id": inbound_id,
            "reply_dry_run": send_result.dry_run if send_result else False,
            "reply_sent": send_result.ok if send_result else False,
            "handoff_required": agent_result.handoff_required,
            "skipped_reply": not bool(send_result),
        }
    )


@router.post("/api/webhooks/brevo/conversations")
async def brevo_conversations_webhook(
    request: Request,
    _: None = Depends(verify_brevo_webhook),
):
    return await handle_brevo_conversations_webhook(request)


@router.post("/api/webhooks/brevo/whatsapp")
async def brevo_whatsapp_webhook(
    request: Request,
    _: None = Depends(verify_brevo_webhook),
):
    return await handle_brevo_conversations_webhook(request)
