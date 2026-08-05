from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.human_takeover import (
    _candidate_keys,
    _conversas_has_takeover_signal,
    _seed_activity_from_row,
    human_takeover_active,
)
from app.models import IncomingMessage


def test_candidate_keys_dedupes():
    msg = IncomingMessage(
        provider="brevo",
        event_type="conversationFragment",
        channel="whatsapp",
        conversation_id="abc",
        sender_key="abc",
        sender_phone="5511999999999",
        visitor_id="vid",
        text="oi",
    )
    keys = _candidate_keys(msg)
    assert keys[0] == "abc"
    assert "5511999999999" in keys
    assert "vid" in keys
    assert len(keys) == len(set(keys))


def test_takeover_signal_ignores_closed():
    assert _conversas_has_takeover_signal({"status": "closed", "assigned_to": "u1"}) is False
    assert _conversas_has_takeover_signal({"status": "open", "assigned_to": "u1"}) is True
    assert _conversas_has_takeover_signal({"status": "open", "bot_activated": False}) is True
    assert _conversas_has_takeover_signal({"status": "open", "bot_activated": True}) is False


def test_seed_prefers_assigned_at_not_customer_last_message():
    assigned = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    seeded = _seed_activity_from_row(
        {
            "assigned_at": assigned,
            "last_message_at": datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc),
        }
    )
    assert seeded == assigned


def test_human_takeover_expires_after_idle(monkeypatch):
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    from app.config import get_settings

    get_settings.cache_clear()

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-idle",
        sender_key="conv-idle",
        text="oi de novo",
    )
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    with (
        patch(
            "app.human_takeover._fetch_conversas_rows",
            return_value=[{"assigned_to": "agent-1", "bot_activated": False, "status": "open"}],
        ),
        patch(
            "app.human_takeover._load_pause_state",
            return_value={"last_human_activity_at": old},
        ),
        patch("app.human_takeover._upsert_pause_state"),
    ):
        assert human_takeover_active(incoming) is False

    get_settings.cache_clear()


def test_human_takeover_active_within_idle(monkeypatch):
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    from app.config import get_settings

    get_settings.cache_clear()

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-active",
        sender_key="conv-active",
        text="ainda com humano",
    )
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    with (
        patch(
            "app.human_takeover._fetch_conversas_rows",
            return_value=[{"assigned_to": "agent-1", "bot_activated": False, "status": "open"}],
        ),
        patch(
            "app.human_takeover._load_pause_state",
            return_value={"last_human_activity_at": recent},
        ),
        patch("app.human_takeover._upsert_pause_state"),
    ):
        assert human_takeover_active(incoming) is True

    get_settings.cache_clear()
