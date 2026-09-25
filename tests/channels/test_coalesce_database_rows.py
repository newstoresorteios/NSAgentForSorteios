from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.channels import inbound_coalesce as coalesce
from app.models import IncomingMessage


@pytest.mark.parametrize("dictionary", [True, False])
def test_dm_after_story_handles_actual_database_row(monkeypatch, dictionary):
    data = {"id": 941, "text": "qual é esse?",
            "channel_metadata": {"image_url_present": True}, "created_at": "2026-09-25"}
    conn = MagicMock()
    conn.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = (
        data if dictionary else tuple(data.values())
    )
    monkeypatch.setattr(coalesce, "get_conn", lambda: conn)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(database_url="test"))
    incoming = IncomingMessage(channel="instagram", conversation_id="ig:test", text="Boa Noite")
    assert coalesce.is_caption_echo_of_recent_image(incoming) is False
    incoming.text = "qual é esse?"
    assert coalesce.is_caption_echo_of_recent_image(incoming) is True
