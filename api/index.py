"""Vercel FastAPI entrypoint — thin factory + re-exports for test monkeypatches."""

from __future__ import annotations

from app.channels.brevo_client import send_brevo_reply
from app.channels.inbound_coalesce import is_caption_echo_of_recent_image
from app.config import get_settings
from app.db import (
    claim_inbound_message,
    has_successful_agent_response,
    inbound_already_completed,
    inbound_message_exists,
    insert_agent_response,
    insert_inbound_message,
    is_latest_inbound_message,
)
from app.http.app_factory import create_app
from app.http.brevo_webhook import handle_brevo_conversations_webhook
from app.http.health import admin_diagnostics_payload, admin_health, health, test_tray_integration
from app.http.payload import read_request_payload, skip_webhook_event, webhook_event_name
from app.http.version import AGENT_VERSION
from app.identity.repository import find_customer_profile_by_phone
from app.learning.remarketing import sync_remarketing_interaction
from app.message_pipeline import process_incoming_message
from app.ops.conversation_lock import acquire_conversation_lock, release_conversation_lock
from app.security import verify_admin_token, verify_brevo_webhook, verify_remarketing_cron
from app.tray.tray_adapter_client import TrayAdapterClient

app = create_app()

__all__ = [
    "AGENT_VERSION",
    "TrayAdapterClient",
    "acquire_conversation_lock",
    "admin_diagnostics_payload",
    "admin_health",
    "app",
    "claim_inbound_message",
    "find_customer_profile_by_phone",
    "get_settings",
    "handle_brevo_conversations_webhook",
    "has_successful_agent_response",
    "health",
    "inbound_already_completed",
    "inbound_message_exists",
    "insert_agent_response",
    "insert_inbound_message",
    "is_caption_echo_of_recent_image",
    "is_latest_inbound_message",
    "process_incoming_message",
    "read_request_payload",
    "release_conversation_lock",
    "send_brevo_reply",
    "skip_webhook_event",
    "sync_remarketing_interaction",
    "test_tray_integration",
    "verify_admin_token",
    "verify_brevo_webhook",
    "verify_remarketing_cron",
    "webhook_event_name",
]
