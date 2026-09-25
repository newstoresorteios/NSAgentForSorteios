import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.configuration import workspace
from app.ingress import worker
from app.models import IncomingMessage
from app.stories.instagram_story_models import InstagramStoryContext


@pytest.mark.parametrize("known", [True, False])
def test_story_is_scoped_before_marking_processed(monkeypatch, known):
    incoming = IncomingMessage(channel="instagram", conversation_id="ig:test", text="valor?",
        instagram_story=InstagramStoryContext(replied_to_story=True, story_media_id="s1"))
    monkeypatch.setattr(worker, "incoming_from_inbox_payload", lambda _: incoming)
    monkeypatch.setattr(worker, "claim_inbound_message", lambda _: (True, 940))
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(database_url="test"))
    monkeypatch.setattr(workspace, "resolve_conversation_workspace", lambda *a: "workspace" if known else None)
    monkeypatch.setattr("app.persona.persona_runtime.load_persona_runtime", lambda: SimpleNamespace(
        flow_params_dict=lambda: {"workspace_id": "workspace"}))
    calls = []
    monkeypatch.setattr(workspace, "stamp_inbound_workspace", lambda *a: calls.append(("stamp", a)))
    monkeypatch.setattr(worker, "mark_inbox_processed", lambda *a, **kw: calls.append(("processed", a)))
    result = asyncio.run(worker.process_inbox_row({"id": 385, "payload_json": {}}, lock_held=True))
    assert result["skipped"] == "story_message"
    assert calls == [("stamp", (940, "workspace")), ("processed", (385,))]


def test_workspace_failure_retries_without_reply_or_marking_processed(monkeypatch):
    incoming = IncomingMessage(channel="instagram", text="valor?",
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
