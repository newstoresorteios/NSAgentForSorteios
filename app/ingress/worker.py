"""Inbox worker: process leased Meta/Brevo inbound rows through the agent."""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.db import (
    claim_inbound_message,
    has_successful_agent_response,
    insert_agent_response,
)
from app.channels.inbound_coalesce import (
    attach_recent_image_for_followup,
    is_caption_echo_of_recent_image,
)
from app.ingress.inbox import (
    claim_conversation_burst,
    claim_pending_inbox,
    mark_inbox_failed,
    mark_inbox_processed,
)
from app.ingress.outbox import enqueue_accepted_outbound
from app.ingress.reconstruct import incoming_from_inbox_payload
from app.models import AgentResult, IncomingMessage
from app.ops.observability import log_event, log_exception


def _merge_burst_messages(messages: list[IncomingMessage], inbox_ids: list[int]) -> IncomingMessage:
    base = messages[0].model_copy(deep=True)
    texts: list[str] = []
    image_seen = any(bool((item.image_url or "").strip()) for item in messages)
    for item in messages:
        value = str(item.text or "").strip()
        if value and not (image_seen and value.casefold() in {"image", "imagem"}) and value not in texts:
            texts.append(value)
        for field in (
            "image_url", "image_mime_type", "attachment_type", "audio_url",
            "audio_mime_type", "audio_filename", "instagram_story",
        ):
            candidate = getattr(item, field, None)
            if candidate:
                setattr(base, field, candidate)
        if item.message_id:
            base.message_id = item.message_id
    base.text = "\n".join(texts)
    base.input_modality = "image" if base.image_url else base.input_modality
    metadata = dict(base.channel_metadata or {})
    metadata.update({
        "grouped_inbox_ids": inbox_ids,
        "grouped_message_ids": [item.message_id for item in messages if item.message_id],
        "grouped_message_count": len(messages),
    })
    base.channel_metadata = metadata
    raw = dict(base.raw or {})
    raw["grouped_inbox_ids"] = inbox_ids
    raw["grouped_message_count"] = len(messages)
    base.raw = raw
    return base


async def _claim_and_merge_burst(row: dict[str, Any], incoming: IncomingMessage) -> tuple[IncomingMessage, list[int]]:
    from app.configuration.runtime import ConfigurationUnavailable, policy

    # Keep Instagram events separate: merging a Story with a normal DM would
    # either silence the DM or let the Story reach the agent.
    if str(incoming.channel or "").lower() == "instagram":
        return incoming, [int(row["id"])]

    if not str(getattr(get_settings(), "database_url", "") or ""):
        return incoming, [int(row["id"])]
    try:
        raw_window = policy("inboundBurstWindowMs")
        raw_max = policy("inboundBurstMaxMessages")
    except ConfigurationUnavailable:
        try:
            from app.persona.persona_runtime import load_persona_runtime

            runtime = await asyncio.to_thread(load_persona_runtime)
            values = (runtime.configuration_bundle or {}).get("values") or {}
            raw_window = values["inboundBurstWindowMs"]
            raw_max = values["inboundBurstMaxMessages"]
        except (KeyError, RuntimeError, TypeError, ValueError):
            return incoming, [int(row["id"])]
    try:
        window_ms = max(0, min(int(raw_window), 15000))
        max_messages = max(1, min(int(raw_max), 25))
    except (TypeError, ValueError):
        return incoming, [int(row["id"])]
    if window_ms <= 0 or max_messages <= 1:
        return incoming, [int(row["id"])]
    await asyncio.sleep(window_ms / 1000)
    followups = claim_conversation_burst(
        int(row["id"]), window_ms=window_ms, limit=max_messages,
    )
    parsed = [incoming]
    ids = [int(row["id"])]
    for followup in followups:
        ids.append(int(followup["id"]))
        candidate = incoming_from_inbox_payload(followup.get("payload_json"))
        if candidate is not None:
            parsed.append(candidate)
    if len(parsed) == 1:
        return incoming, ids
    merged = _merge_burst_messages(parsed, ids)
    log_event("inbox.burst_grouped", {
        "anchor_inbox_id": int(row["id"]), "inbox_ids": ids,
        "message_count": len(parsed), "window_ms": window_ms,
    })
    return merged, ids


def _mark_group_processed(inbox_ids: list[int], inbound_id: int | None = None) -> None:
    for grouped_id in inbox_ids:
        mark_inbox_processed(grouped_id, processed_inbound_id=inbound_id)


def _mark_group_failed(inbox_ids: list[int], *, error: str, dead: bool = False) -> None:
    for grouped_id in inbox_ids:
        mark_inbox_failed(grouped_id, error=error, dead=dead)


async def _customer_context_for(incoming: IncomingMessage) -> dict[str, Any]:
    if incoming.sender_phone:
        from app.identity.repository import find_customer_profile_by_phone

        return find_customer_profile_by_phone(incoming.sender_phone)
    return {
        "found": False,
        "channel": incoming.channel,
        "sender_key": incoming.sender_key,
        "display_name": incoming.sender_name,
    }


async def _send_reply(incoming: IncomingMessage, result: AgentResult) -> dict[str, Any]:
    provider = (incoming.provider or "").lower()
    if provider == "meta" or (not provider and
        (incoming.channel or "").lower() == "instagram"
        and str(getattr(get_settings(), "instagram_ingress_provider", "meta")).lower()
        in {"meta", "dual"}
    ):
        from app.channels.meta_instagram import send_meta_instagram_reply

        return await send_meta_instagram_reply(incoming, result)

    from app.channels.brevo_client import send_brevo_reply

    send_result = await send_brevo_reply(incoming, result)
    return {
        "ok": bool(send_result.ok),
        "status_code": send_result.status_code,
        "provider_response": send_result.model_dump(),
        "error": send_result.error,
    }


def _sync_remarketing_after_delivery(
    incoming: IncomingMessage,
    result: AgentResult,
    inbound_id: int | None,
) -> None:
    """Apply the same remarketing lifecycle used by synchronous webhooks."""
    try:
        from app.learning.remarketing import sync_remarketing_interaction

        sync_remarketing_interaction(
            incoming,
            inbound_id=inbound_id,
            response_metadata=result.response_metadata,
            handoff_required=result.handoff_required,
        )
        log_event(
            "remarketing.async_synced",
            {"inbound_id": inbound_id, "channel": incoming.channel},
        )
    except Exception as exc:  # noqa: BLE001
        log_exception(
            "remarketing.async_sync_failed",
            exc,
            {"inbound_id": inbound_id, "channel": incoming.channel},
        )


async def process_inbox_row(row: dict[str, Any], *, lock_held: bool = False) -> dict[str, Any]:
    """Serialize every worker entrance, including cron and Meta delivery."""
    from app.ops.conversation_lock import (
        acquire_conversation_lock, release_conversation_lock, conversation_lock_key,
    )
    incoming = incoming_from_inbox_payload(row.get("payload_json"))
    if incoming is None or lock_held:
        return await _process_inbox_row_locked(row)
    key = conversation_lock_key(
        conversation_id=incoming.conversation_id or row.get("conversation_key"),
        sender_key=incoming.sender_key, sender_phone=incoming.sender_phone,
        visitor_id=incoming.visitor_id,
    )
    if not key:
        mark_inbox_failed(int(row["id"]), error="missing_conversation_identity", dead=True)
        return {"ok": False, "error": "missing_conversation_identity"}
    settings = get_settings()
    handle = await acquire_conversation_lock(
        key, database_url=str(getattr(settings, "database_url", "") or ""),
        timeout_seconds=float(getattr(settings, "agent_conversation_lock_timeout_seconds", 15) or 15),
    )
    try:
        return await _process_inbox_row_locked(row)
    finally:
        await release_conversation_lock(handle)


async def _process_inbox_row_locked(row: dict[str, Any]) -> dict[str, Any]:
    inbox_id = int(row["id"])
    incoming = incoming_from_inbox_payload(row.get("payload_json"))
    if incoming is None:
        mark_inbox_failed(inbox_id, error="invalid_inbox_payload", dead=True)
        return {"ok": False, "inbox_id": inbox_id, "error": "invalid_inbox_payload"}
    if not incoming.conversation_id:
        row_cid = str(row.get("conversation_key") or "").strip()
        if row_cid:
            incoming.conversation_id = row_cid

    incoming, grouped_inbox_ids = await _claim_and_merge_burst(row, incoming)

    from app.stories.instagram_story_intent import should_silence_story_message
    silence_story = should_silence_story_message(incoming)

    if not silence_story and not (incoming.image_url or "").strip() and is_caption_echo_of_recent_image(
        incoming
    ):
        _mark_group_processed(grouped_inbox_ids)
        log_event(
            "inbox.skipped_caption_echo",
            {"inbox_id": inbox_id, "channel": incoming.channel},
        )
        return {"ok": True, "inbox_id": inbox_id, "skipped": "caption_echo"}

    if not silence_story:
        incoming = attach_recent_image_for_followup(incoming)

    try:
        claimed, inbound_id = claim_inbound_message(incoming.model_dump(mode="json"))
    except Exception as exc:
        log_exception("inbox.claim_inbound_failed", exc, {"inbox_id": inbox_id})
        _mark_group_failed(grouped_inbox_ids, error=type(exc).__name__)
        return {"ok": False, "inbox_id": inbox_id, "error": "claim_failed"}

    if not claimed:
        if inbound_id and has_successful_agent_response(inbound_id):
            _mark_group_processed(grouped_inbox_ids, inbound_id)
            return {"ok": True, "inbox_id": inbox_id, "skipped": "duplicate_message"}
        if not inbound_id:
            _mark_group_failed(grouped_inbox_ids, error="duplicate_without_inbound_id")
            return {
                "ok": False,
                "inbox_id": inbox_id,
                "error": "duplicate_without_inbound_id",
            }

    if isinstance(incoming.raw, dict):
        incoming.raw["inbound_id"] = inbound_id
        incoming.raw["inbox_id"] = inbox_id

    # Persist all Story input for the Central, but never analyze or reply to it.
    if silence_story:
        from app.configuration.workspace import stamp_silent_inbound_workspace
        try:
            await asyncio.to_thread(stamp_silent_inbound_workspace, incoming, inbound_id)
        except Exception as exc:
            log_exception("inbox.story_workspace_failed", exc, {"inbox_id": inbox_id})
            _mark_group_failed(grouped_inbox_ids, error="story_workspace_failed")
            return {"ok": False, "inbox_id": inbox_id, "error": "story_workspace_failed"}
        _mark_group_processed(grouped_inbox_ids, inbound_id)
        log_event(
            "inbox.skipped_story_message",
            {"inbox_id": inbox_id, "channel": incoming.channel, "inbound_id": inbound_id},
        )
        return {
            "ok": True,
            "inbox_id": inbox_id,
            "inbound_id": inbound_id,
            "skipped": "story_message",
        }

    try:
        from app.ops.human_takeover import human_takeover_active

        if human_takeover_active(incoming):
            _mark_group_processed(grouped_inbox_ids, inbound_id)
            log_event(
                "inbox.skipped_human_takeover",
                {"inbox_id": inbox_id, "channel": incoming.channel, "inbound_id": inbound_id},
            )
            return {
                "ok": True,
                "inbox_id": inbox_id,
                "inbound_id": inbound_id,
                "skipped": "human_takeover",
            }
    except Exception as exc:  # noqa: BLE001
        log_exception(
            "inbox.human_takeover_check_failed",
            exc,
            {"inbox_id": inbox_id},
        )

    from app.ingress.outbox import has_sent_outbound
    if has_successful_agent_response(inbound_id) or has_sent_outbound(inbound_id):
        _mark_group_processed(grouped_inbox_ids, inbound_id)
        return {"ok": True, "inbox_id": inbox_id, "skipped": "already_sent"}

    from app.llm.llm_call_policy import build_llm_call_budget
    from app.message_pipeline import process_incoming_message
    from app.ops.runtime_context import reset_current_turn, set_current_turn
    from app.ops.turn_runtime import LLMCallBudget, TurnRuntimeContext

    from app.ingress.outbox import get_accepted_outbound, result_from_outbox_row
    accepted = get_accepted_outbound(inbound_id)
    customer_context = await _customer_context_for(incoming) if accepted is None else {}
    budget_cfg = build_llm_call_budget(execution_path="normal")
    turn = TurnRuntimeContext(
        trace_id=f"inbox-{inbox_id}",
        llm_budget=LLMCallBudget(
            max_calls=int(budget_cfg.get("max_calls") or 2),
            enforce=True,
        ),
    )
    turn.execution_path = str(budget_cfg.get("execution_path") or "normal")
    token = set_current_turn(turn)
    try:
        result = (result_from_outbox_row(accepted) if accepted is not None
                  else await process_incoming_message(incoming, customer_context))
    finally:
        reset_current_turn(token)
    outbox_id = enqueue_accepted_outbound(
        incoming=incoming,
        result=result,
        inbox_id=inbox_id,
        inbound_id=inbound_id,
    )
    from app.ingress.outbox import dispatch_accepted_outbound
    if outbox_id is not None:
        send_info = await dispatch_accepted_outbound(outbox_id, _send_reply)
    else:
        send_info = await _send_reply(incoming, result)
    send_ok = bool(send_info.get("ok"))
    if send_info.get("queued"):
        _mark_group_processed(grouped_inbox_ids, inbound_id)
        return {"ok": True, "inbox_id": inbox_id, "inbound_id": inbound_id, "queued": True}

    try:
        response_id = insert_agent_response(
            {
                "inbound_id": inbound_id,
                "channel": incoming.channel,
                "sender_key": incoming.sender_key,
                "sender_phone": incoming.sender_phone,
                "reply_text": result.reply_text,
                "intent": result.intent,
                "handoff_required": result.handoff_required,
                "safety_reason": result.safety_reason,
                "response_metadata": result.response_metadata,
                "provider_send_ok": send_ok,
                "provider_response": send_info,
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
        except Exception as exc:  # noqa: BLE001
            log_exception(
                "inbox.pipeline_review_bind_failed",
                exc,
                {"inbox_id": inbox_id, "inbound_id": inbound_id},
            )
    except Exception as exc:  # noqa: BLE001
        log_exception(
            "inbox.response_persist_failed",
            exc,
            {"inbox_id": inbox_id, "inbound_id": inbound_id},
        )

    if not send_ok:
        _mark_group_failed(
            grouped_inbox_ids,
            error=str(send_info.get("error") or "send_failed"),
        )
        return {
            "ok": False,
            "inbox_id": inbox_id,
            "inbound_id": inbound_id,
            "error": "send_failed",
            "send": send_info,
        }

    _sync_remarketing_after_delivery(incoming, result, inbound_id)
    _mark_group_processed(grouped_inbox_ids, inbound_id)
    log_event(
        "inbox.agent_turn_completed",
        {
            "inbox_id": inbox_id,
            "inbound_id": inbound_id,
            "channel": incoming.channel,
            "provider": incoming.provider,
            "image_url_present": bool((incoming.image_url or "").strip()),
            "story_present": incoming.instagram_story is not None,
            "grouped_message_count": len(grouped_inbox_ids),
            "reply_chars": len(result.reply_text or ""),
        },
    )
    return {
        "ok": True,
        "inbox_id": inbox_id,
        "inbound_id": inbound_id,
        "send": send_info,
    }


async def process_inbox_batch(
    *,
    limit: int | None = None,
    lease_seconds: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    batch = int(limit or getattr(settings, "agent_inbox_batch_size", 5) or 5)
    lease = int(
        lease_seconds or getattr(settings, "agent_inbox_lease_seconds", 120) or 120
    )
    rows = claim_pending_inbox(limit=batch, lease_seconds=lease)
    processed = 0
    failed = 0
    results: list[dict[str, Any]] = []
    for row in rows:
        inbox_id = int(row["id"])
        try:
            item = await process_inbox_row(row)
            results.append(item)
            if item.get("ok"):
                processed += 1
            else:
                failed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            attempts = int(row.get("attempts") or 1)
            log_exception(
                "inbox.worker_item_failed",
                exc,
                {"inbox_id": inbox_id},
            )
            mark_inbox_failed(
                inbox_id,
                error=type(exc).__name__,
                dead=attempts >= 8,
            )
            results.append(
                {
                    "ok": False,
                    "inbox_id": inbox_id,
                    "error": type(exc).__name__,
                }
            )
    return {
        "ok": True,
        "claimed": len(rows),
        "processed": processed,
        "failed": failed,
        "results": results,
    }
