"""Recover Instagram profile pictures for recent Central conversations."""

from __future__ import annotations

from typing import Any

from app.channels.meta_instagram import _lookup_ig_profile
from app.ops.observability import log_event


def backfill_instagram_profile_pictures(
    *, limit: int = 25, max_age_days: int = 30,
) -> dict[str, Any]:
    """Refresh recent Instagram contacts still missing an avatar.

    Meta only exposes a messaging user's profile during its permitted access
    window, so this intentionally prioritizes the newest conversations.
    """
    from app.db import get_conn

    safe_limit = max(1, min(int(limit), 50))
    safe_days = max(1, min(int(max_age_days), 90))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (conversation_id)
                           id, conversation_id, workspace_id, sender_external_id,
                           visitor_id, sender_username, channel_metadata, created_at
                    FROM public.ai_inbound_messages
                    WHERE channel = 'instagram'
                      AND workspace_id IS NOT NULL
                      AND conversation_id IS NOT NULL
                      AND created_at >= NOW() - (%(max_age_days)s * INTERVAL '1 day')
                    ORDER BY conversation_id, created_at DESC
                )
                SELECT inbound.id, inbound.conversation_id, inbound.workspace_id,
                       inbound.sender_external_id, inbound.visitor_id,
                       inbound.sender_username,
                       (conversation.customer_avatar IS NULL) AS avatar_missing
                FROM latest AS inbound
                JOIN public.conversas AS conversation
                  ON conversation.workspace_id = inbound.workspace_id
                 AND conversation.channel = 'instagram'
                 AND conversation.external_thread_id = inbound.conversation_id
                 AND conversation.merged_into IS NULL
                WHERE (
                      NULLIF(inbound.channel_metadata->>'profile_picture_checked_at', '') IS NULL
                      OR (inbound.channel_metadata->>'profile_picture_checked_at')::timestamptz
                         <= NOW() - INTERVAL '1 day'
                  )
                ORDER BY
                    (conversation.customer_avatar IS NULL) DESC,
                    NULLIF(inbound.channel_metadata->>'profile_picture_checked_at', '') NULLS FIRST,
                    inbound.created_at DESC
                LIMIT %(limit)s
                """,
                {"limit": safe_limit, "max_age_days": safe_days},
            )
            candidates = list(cur.fetchall() or [])

    updated = 0
    unavailable = 0
    failed = 0
    for candidate in candidates:
        sender_id = str(
            candidate.get("sender_external_id")
            or candidate.get("visitor_id")
            or ""
        ).strip()
        if not sender_id:
            conversation_id = str(candidate.get("conversation_id") or "")
            sender_id = conversation_id[3:] if conversation_id.startswith("ig:") else ""
        if not sender_id:
            unavailable += 1
            continue
        try:
            profile = _lookup_ig_profile(sender_id, force=True)
            picture = profile.get("profile_picture_url")
            profile_link = profile.get("profile_url")
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.ai_inbound_messages
                        SET channel_metadata = COALESCE(channel_metadata, '{}'::jsonb)
                            || jsonb_build_object('profile_picture_checked_at', NOW())
                            || CASE WHEN %(picture)s IS NOT NULL
                                    THEN jsonb_build_object('profile_picture_url', %(picture)s)
                                    ELSE '{}'::jsonb END,
                            source_channel_link = COALESCE(%(profile_link)s, source_channel_link)
                        WHERE id = %(inbound_id)s
                          AND workspace_id = %(workspace_id)s::uuid
                        """,
                        {
                            "picture": picture,
                            "profile_link": profile_link,
                            "inbound_id": candidate["id"],
                            "workspace_id": str(candidate["workspace_id"]),
                        },
                    )
                    if picture:
                        cur.execute(
                            """
                            UPDATE public.conversas
                            SET customer_avatar = %(picture)s,
                                updated_at = NOW()
                            WHERE workspace_id = %(workspace_id)s::uuid
                              AND channel = 'instagram'
                              AND external_thread_id = %(conversation_id)s
                              AND merged_into IS NULL
                            """,
                            {
                                "picture": picture,
                                "workspace_id": str(candidate["workspace_id"]),
                                "conversation_id": str(candidate["conversation_id"]),
                            },
                        )
                conn.commit()
            if picture:
                updated += 1
            else:
                unavailable += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log_event(
                "meta.profile_backfill.error",
                {"error_type": type(exc).__name__},
            )

    result = {
        "ok": failed == 0,
        "scanned": len(candidates),
        "updated": updated,
        "unavailable": unavailable,
        "failed": failed,
    }
    log_event("meta.profile_backfill.completed", result)
    return result
