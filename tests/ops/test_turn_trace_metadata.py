from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app import message_pipeline
from app.core import db
from app.models import AgentResult, IncomingMessage
from app.ops.runtime_context import reset_current_turn, set_current_turn
from app.ops.turn_runtime import TurnRuntimeContext
from app.persona import persona_runtime
from app.persona.persona_runtime import PersonaRuntimeConfig


@pytest.mark.asyncio
async def test_pipeline_attaches_safe_turn_summary(monkeypatch):
    runtime = TurnRuntimeContext(trace_id="trace-ui")
    runtime.openai_call_count = 1
    runtime.stage_durations_ms["catalog"] = 12.5
    token = set_current_turn(runtime)
    monkeypatch.setattr(
        persona_runtime,
        "load_persona_runtime",
        lambda: PersonaRuntimeConfig(
            loaded=True,
            workspace_id="22222222-2222-2222-2222-222222222222",
        ),
    )

    async def fake_process(_incoming, _context):
        return AgentResult(reply_text="Resposta segura")

    monkeypatch.setattr(message_pipeline, "_process_incoming_message", fake_process)
    try:
        result = await message_pipeline.process_incoming_message(
            IncomingMessage(text="Olá"),
            {},
        )
    finally:
        reset_current_turn(token)

    summary = result.response_metadata["turn_runtime"]
    assert summary["trace_id"] == "trace-ui"
    assert summary["openai_call_count"] == 1
    assert summary["stage_durations_ms"]["catalog"] == 12.5
    assert "conversation_key" not in summary
    assert result.response_metadata["persona_runtime"]["workspace_id"] == "22222222-2222-2222-2222-222222222222"


def test_response_persistence_stamps_workspace_from_persona(monkeypatch):
    captured = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _sql, params):
            captured.update(params)

        def fetchone(self):
            return {"id": 9}

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def fake_conn():
        yield Connection()

    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(database_url="postgres://test"))
    monkeypatch.setattr(db, "ensure_tables", lambda: None)
    monkeypatch.setattr(db, "get_conn", fake_conn)

    response_id = db.insert_agent_response({
        "reply_text": "ok",
        "response_metadata": {
            "persona_runtime": {
                "workspace_id": "22222222-2222-2222-2222-222222222222",
            }
        },
    })

    assert response_id == 9
    assert captured["workspace_id"] == "22222222-2222-2222-2222-222222222222"
