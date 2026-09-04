from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.ops.human_takeover import (
    _candidate_keys,
    _conversas_has_takeover_signal,
    _human_activity_from_row,
    _is_phone_fallback_stale,
    _is_stale_conversa,
    _lookup_keys,
    _pick_takeover_row,
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


def test_lookup_keys_adds_whatsapp_phone_aliases():
    msg = IncomingMessage(
        channel="whatsapp",
        conversation_id="8TP3QuTjwzgLQp935",
        sender_key="whatsapp:5548999490859",
        sender_phone="5548999490859",
        text="oi",
    )
    keys = _lookup_keys(msg)
    assert "5548999490859" in keys
    assert "whatsapp:5548999490859" in keys
    assert "8TP3QuTjwzgLQp935" in keys


def test_pick_takeover_row_prefers_current_thread():
    rows = [
        {
            "external_thread_id": "old-thread",
            "bot_activated": False,
            "status": "waiting",
        },
        {
            "external_thread_id": "current-thread",
            "bot_activated": False,
            "status": "active",
        },
    ]
    picked = _pick_takeover_row(rows, conversation_id="current-thread")
    assert picked is not None
    assert picked["external_thread_id"] == "current-thread"


def test_takeover_signal_ignores_closed():
    assert _conversas_has_takeover_signal({"status": "closed", "assigned_to": "u1"}) is False
    assert _conversas_has_takeover_signal({"status": "open", "assigned_to": "u1"}) is True
    assert _conversas_has_takeover_signal({"status": "open", "bot_activated": False}) is True
    assert _conversas_has_takeover_signal({"status": "open", "bot_activated": True}) is False


def test_human_activity_ignores_customer_last_message():
    assigned = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    assert (
        _human_activity_from_row(
            {
                "assigned_at": assigned,
                "last_message_at": datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc),
            }
        )
        == assigned
    )
    assert (
        _human_activity_from_row(
            {"last_message_at": datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)}
        )
        is None
    )


def test_bot_deactivated_expires_after_idle(monkeypatch):
    """bot_activated=false alone must NOT mute forever — idle window applies."""
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    from app.config import get_settings

    get_settings.cache_clear()

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-bot-off",
        sender_key="conv-bot-off",
        text="oi",
    )
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    with (
        patch(
            "app.ops.human_takeover._fetch_conversas_rows",
            return_value=[
                {
                    "assigned_to": None,
                    "bot_activated": False,
                    "status": "waiting",
                    "external_thread_id": "conv-bot-off",
                }
            ],
        ),
        patch(
            "app.ops.human_takeover._load_pause_state",
            return_value={"last_human_activity_at": old},
        ),
        patch("app.ops.human_takeover._upsert_pause_state"),
    ):
        assert human_takeover_active(incoming) is False

    get_settings.cache_clear()


def test_bot_deactivated_mutes_within_idle(monkeypatch):
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    from app.config import get_settings

    get_settings.cache_clear()

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-bot-off",
        sender_key="conv-bot-off",
        text="oi",
    )
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    with (
        patch(
            "app.ops.human_takeover._fetch_conversas_rows",
            return_value=[
                {
                    "assigned_to": None,
                    "bot_activated": False,
                    "status": "waiting",
                    "external_thread_id": "conv-bot-off",
                }
            ],
        ),
        patch(
            "app.ops.human_takeover._load_pause_state",
            return_value={"last_human_activity_at": recent},
        ),
        patch("app.ops.human_takeover._upsert_pause_state"),
    ):
        assert human_takeover_active(incoming) is True

    get_settings.cache_clear()


def test_stuck_assigned_without_activity_allows_bot_when_persist_fails(monkeypatch):
    """If we cannot store the idle clock, never mute permanently."""
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    from app.config import get_settings

    get_settings.cache_clear()

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-stuck",
        sender_key="conv-stuck",
        text="oi",
    )
    with (
        patch(
            "app.ops.human_takeover._fetch_conversas_rows",
            return_value=[
                {
                    "assigned_to": "agent-1",
                    "bot_activated": True,
                    "status": "open",
                }
            ],
        ),
        patch("app.ops.human_takeover._load_pause_state", return_value=None),
        patch(
            "app.ops.human_takeover._upsert_pause_state",
            side_effect=RuntimeError("no table"),
        ),
    ):
        assert human_takeover_active(incoming) is False

    get_settings.cache_clear()


def test_first_observation_mutes_only_when_persisted(monkeypatch):
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    from app.config import get_settings

    get_settings.cache_clear()

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-first",
        sender_key="conv-first",
        text="oi",
    )
    with (
        patch(
            "app.ops.human_takeover._fetch_conversas_rows",
            return_value=[
                {
                    "assigned_to": "agent-1",
                    "bot_activated": True,
                    "status": "open",
                }
            ],
        ),
        patch("app.ops.human_takeover._load_pause_state", return_value=None),
        patch("app.ops.human_takeover._upsert_pause_state") as upsert,
    ):
        assert human_takeover_active(incoming) is True
        assert upsert.called

    get_settings.cache_clear()


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
            "app.ops.human_takeover._fetch_conversas_rows",
            return_value=[
                {
                    "assigned_to": "agent-1",
                    "bot_activated": True,
                    "status": "open",
                }
            ],
        ),
        patch(
            "app.ops.human_takeover._load_pause_state",
            return_value={"last_human_activity_at": old},
        ),
        patch("app.ops.human_takeover._upsert_pause_state"),
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
            "app.ops.human_takeover._fetch_conversas_rows",
            return_value=[
                {
                    "assigned_to": "agent-1",
                    "bot_activated": False,
                    "status": "open",
                }
            ],
        ),
        patch(
            "app.ops.human_takeover._load_pause_state",
            return_value={"last_human_activity_at": recent},
        ),
        patch("app.ops.human_takeover._upsert_pause_state"),
    ):
        assert human_takeover_active(incoming) is True

    get_settings.cache_clear()


def test_is_stale_conversa_by_days(monkeypatch):
    monkeypatch.setenv("HUMAN_TAKEOVER_STALE_CONVERSA_DAYS", "7")
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    from app.config import get_settings

    get_settings.cache_clear()

    old = datetime.now(timezone.utc) - timedelta(days=8)
    assert _is_stale_conversa({"last_message_at": old}) is True
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    assert _is_stale_conversa({"last_message_at": recent}) is False

    get_settings.cache_clear()


def test_is_stale_conversa_by_idle_buffer(monkeypatch):
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    monkeypatch.setenv("HUMAN_TAKEOVER_STALE_CONVERSA_DAYS", "30")
    from app.config import get_settings

    get_settings.cache_clear()

    borderline = datetime.now(timezone.utc) - timedelta(minutes=25)
    assert _is_phone_fallback_stale({"updated_at": borderline}) is True
    active = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert _is_phone_fallback_stale({"updated_at": active}) is False

    get_settings.cache_clear()


def test_pick_takeover_row_ignores_stale_phone_match(monkeypatch):
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    monkeypatch.setenv("HUMAN_TAKEOVER_STALE_CONVERSA_DAYS", "7")
    from app.config import get_settings

    get_settings.cache_clear()

    stale_at = datetime.now(timezone.utc) - timedelta(days=10)
    rows = [
        {
            "external_thread_id": "old-thread",
            "assigned_to": "agent-1",
            "bot_activated": False,
            "status": "open",
            "last_message_at": stale_at,
        },
    ]
    picked = _pick_takeover_row(rows, conversation_id="new-brevo-thread")
    assert picked is None

    get_settings.cache_clear()


def test_phone_with_only_stale_row_allows_bot(monkeypatch):
    """Regression: stale assigned_to on old thread must not mute new Brevo conversation."""
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    monkeypatch.setenv("HUMAN_TAKEOVER_STALE_CONVERSA_DAYS", "7")
    from app.config import get_settings

    get_settings.cache_clear()

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="new-brevo-thread",
        sender_key="whatsapp:5548999490859",
        sender_phone="5548999490859",
        text="oi",
    )
    stale_at = datetime.now(timezone.utc) - timedelta(days=30)
    with (
        patch(
            "app.ops.human_takeover._fetch_conversas_rows",
            return_value=[
                {
                    "external_thread_id": "old-thread-aug5",
                    "assigned_to": "agent-1",
                    "bot_activated": False,
                    "status": "open",
                    "last_message_at": stale_at,
                    "contact_phone": "5548999490859",
                }
            ],
        ),
        patch("app.ops.human_takeover._load_pause_state", return_value=None),
        patch("app.ops.human_takeover._upsert_pause_state"),
    ):
        assert human_takeover_active(incoming) is False

    get_settings.cache_clear()


def test_current_thread_takeover_still_blocks(monkeypatch):
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    from app.config import get_settings

    get_settings.cache_clear()

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="current-thread",
        sender_key="current-thread",
        text="oi",
    )
    recent = datetime.now(timezone.utc) - timedelta(minutes=3)
    with (
        patch(
            "app.ops.human_takeover._fetch_conversas_rows",
            return_value=[
                {
                    "external_thread_id": "current-thread",
                    "assigned_to": "agent-1",
                    "bot_activated": False,
                    "status": "open",
                    "last_message_at": recent,
                }
            ],
        ),
        patch(
            "app.ops.human_takeover._load_pause_state",
            return_value={"last_human_activity_at": recent},
        ),
        patch("app.ops.human_takeover._upsert_pause_state"),
    ):
        assert human_takeover_active(incoming) is True

    get_settings.cache_clear()
