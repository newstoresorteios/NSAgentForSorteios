from unittest.mock import MagicMock, patch

from app.handoff_queue import mark_conversa_for_human_handoff
from app.models import IncomingMessage


def test_mark_conversa_for_human_handoff_updates_waiting():
    incoming = IncomingMessage(
        channel="whatsapp",
        sender_phone="5521999999999",
        conversation_id="thread-abc",
    )
    cursor = MagicMock()
    cursor.fetchone.side_effect = [
        {"?column?": 1},
    ]
    cursor.fetchall.side_effect = [
        [
            {"column_name": "status"},
            {"column_name": "bot_activated"},
            {"column_name": "assigned_to"},
            {"column_name": "updated_at"},
            {"column_name": "external_thread_id"},
            {"column_name": "contact_phone"},
        ],
        [{"id": "conv-1"}],
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("app.handoff_queue.get_conn") as get_conn:
        get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        get_conn.return_value.__exit__ = MagicMock(return_value=False)
        updated = mark_conversa_for_human_handoff(
            incoming,
            reason="customer_requested_human",
        )

    assert updated == ["conv-1"]
    executed_sql = cursor.execute.call_args_list[-1][0][0]
    executed_params = cursor.execute.call_args_list[-1][0][1]
    assert "status = %s" in executed_sql
    assert "assigned_to = NULL" in executed_sql
    assert executed_params[0] == "waiting"


def test_mark_conversa_for_human_handoff_no_keys():
    incoming = IncomingMessage(channel="whatsapp", text="oi")
    assert mark_conversa_for_human_handoff(incoming, reason="handoff") == []
