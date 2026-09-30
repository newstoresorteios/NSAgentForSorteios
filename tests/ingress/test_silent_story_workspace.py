import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.configuration import workspace
from app.ingress import worker
from app.models import IncomingMessage
from app.stories.instagram_story_models import InstagramStoryContext


def test_story_is_scoped_before_marking_processed(monkeypatch):
    incoming = IncomingMessage(channel="instagram", conversation_id="ig:test", text="🔥",
        instagram_story=InstagramStoryContext(replied_to_story=True, story_media_id="s1"))
    monkeypatch.setattr(worker, "incoming_from_inbox_payload", lambda _: incoming)
    monkeypatch.setattr(worker, "claim_inbound_message", lambda _: (True, 940))
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(database_url="test"))
    monkeypatch.setattr(workspace, "resolve_ingress_workspace", lambda *a: "workspace")
    calls = []
    monkeypatch.setattr(workspace, "stamp_inbound_workspace", lambda *a: calls.append(("stamp", a)))
    monkeypatch.setattr(worker, "mark_inbox_processed", lambda *a, **kw: calls.append(("processed", a)))
    result = asyncio.run(worker.process_inbox_row({"id": 385, "payload_json": {}}, lock_held=True))
    assert result["skipped"] == "story_message"
    assert calls == [("stamp", (940, "workspace")), ("processed", (385,))]


def test_workspace_failure_retries_without_reply_or_marking_processed(monkeypatch):
    incoming = IncomingMessage(channel="instagram", text="🔥",
        instagram_story=InstagramStoryContext(replied_to_story=True))
    monkeypatch.setattr(worker, "incoming_from_inbox_payload", lambda _: incoming)
    monkeypatch.setattr(worker, "claim_inbound_message", lambda _: (True, 940))
    monkeypatch.setattr(workspace, "stamp_silent_inbound_workspace", Mock(side_effect=ValueError("unresolved")))
    processed = Mock()
    failed = Mock()
    monkeypatch.setattr(worker, "mark_inbox_processed", processed)
    monkeypatch.setattr(worker, "mark_inbox_failed", failed)
    result = asyncio.run(worker.process_inbox_row({"id": 385, "payload_json": {}}, lock_held=True))
    assert result["error"] == "story_workspace_failed"
    processed.assert_not_called()
    failed.assert_called_once()


def test_actionable_story_is_scoped_and_reaches_pipeline(monkeypatch):
    incoming = IncomingMessage(channel="instagram", conversation_id="ig:test", text="valor?",
        image_url='https://lookaside.fbsbx.com/story',
        instagram_story=InstagramStoryContext(replied_to_story=True, story_media_id="s1"))
    monkeypatch.setattr(worker, "incoming_from_inbox_payload", lambda _: incoming)
    monkeypatch.setattr(worker, "claim_inbound_message", lambda _: (True, 940))
    monkeypatch.setattr(worker, "is_caption_echo_of_recent_image", lambda _: False)
    monkeypatch.setattr(worker, "attach_recent_image_for_followup", lambda value: value)
    monkeypatch.setattr(worker, "has_successful_agent_response", lambda _: False)
    monkeypatch.setattr("app.ops.human_takeover.human_takeover_active", lambda _: False)
    monkeypatch.setattr("app.ingress.outbox.has_sent_outbound", lambda _: False)
    monkeypatch.setattr("app.ingress.outbox.get_accepted_outbound", lambda _: None)
    monkeypatch.setattr(worker, "_customer_context_for", AsyncMock(return_value={}))
    calls = []
    monkeypatch.setattr(workspace, "stamp_silent_inbound_workspace", lambda *args: calls.append('scoped'))
    async def archive(inbound_id):
        assert inbound_id == 940
        assert calls == ['scoped']
        calls.append('archived')
    monkeypatch.setattr('app.ops.instagram_media_archive.archive_instagram_inbound_media', archive)
    class ReachedPipeline(Exception):
        pass
    async def pipeline(value, context):
        assert calls == ['scoped', 'archived']
        assert value.text == 'valor?'
        raise ReachedPipeline
    monkeypatch.setattr("app.message_pipeline.process_incoming_message", pipeline)
    processed = Mock()
    monkeypatch.setattr(worker, "mark_inbox_processed", processed)
    with pytest.raises(ReachedPipeline):
        asyncio.run(worker.process_inbox_row({"id": 385, "payload_json": {}}, lock_held=True))
    processed.assert_not_called()
