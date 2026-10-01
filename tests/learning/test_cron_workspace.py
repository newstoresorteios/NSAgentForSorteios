from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.configuration.runtime import current_bundle, effective_settings
from app.persona.persona_runtime import get_persona_runtime
from app.learning import cron_context


@pytest.mark.asyncio
async def test_scheduled_job_binds_persona_and_settings_and_resets_on_failure(monkeypatch):
    previous_bundle = current_bundle()
    previous_persona = get_persona_runtime()
    previous_settings = effective_settings()
    persona = SimpleNamespace(workspace_id="workspace-a", configuration_bundle={"values": {"learningEnabled": True}})
    settings = object()
    monkeypatch.setattr(cron_context, "load_scheduled_persona", lambda: persona)
    monkeypatch.setattr(cron_context, "settings_from_bundle", lambda *_args: settings)
    with pytest.raises(RuntimeError, match="job failed"):
        async with cron_context.scheduled_workspace():
            assert get_persona_runtime() is persona
            assert current_bundle()["workspace_id"] == "workspace-a"
            assert effective_settings() is settings
            raise RuntimeError("job failed")
    assert current_bundle() == previous_bundle
    assert get_persona_runtime() is previous_persona
    assert effective_settings() is previous_settings


@pytest.mark.parametrize("rows", [[], [{"workspace_id": "a"}, {"workspace_id": "b"}]])
def test_scheduled_job_refuses_missing_or_ambiguous_workspace(monkeypatch, rows):
    connection = MagicMock()
    connection.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = rows
    monkeypatch.setattr(cron_context, "get_conn", lambda: connection)
    with pytest.raises(RuntimeError, match="scheduled_workspace_missing_or_ambiguous"):
        cron_context.load_scheduled_persona()


@pytest.mark.asyncio
async def test_remarketing_preserves_meta_provider_and_workspace(monkeypatch):
    from app.learning import remarketing
    from app.channels import meta_instagram
    from unittest.mock import AsyncMock

    monkeypatch.setattr(remarketing, "get_settings", lambda: SimpleNamespace(remarketing_enabled=True, remarketing_batch_size=1))
    monkeypatch.setattr(remarketing, "claim_due_remarketing_attempts", lambda _limit: [{
        "id": 1, "conversation_status_id": 2, "provider": "meta", "workspace_id": "workspace-a",
        "channel": "instagram", "sender_external_id": "customer-1", "touch_number": 1,
    }])
    monkeypatch.setattr(remarketing, "remarketing_attempt_is_sendable", lambda *_a, **_k: True)
    finish = MagicMock()
    monkeypatch.setattr(remarketing, "finish_remarketing_attempt", finish)
    meta_send = AsyncMock(return_value={"ok": True, "provider_response": {"message_id": "receipt-1"}})
    brevo_send = AsyncMock(side_effect=AssertionError("must use Meta"))
    monkeypatch.setattr(meta_instagram, "send_meta_instagram_reply", meta_send)
    monkeypatch.setattr(remarketing, "send_brevo_reply", brevo_send)
    assert await remarketing.run_remarketing_batch() == {"claimed": 1, "sent": 1, "failed": 0}
    incoming, result = meta_send.call_args.args
    assert incoming.provider == "meta"
    assert incoming.raw["workspace_id"] == "workspace-a"
    assert incoming.sender_external_id == "customer-1"
    assert result.reply_text
    assert finish.call_args.kwargs["provider_response"]["provider_response"]["message_id"] == "receipt-1"
    brevo_send.assert_not_awaited()
