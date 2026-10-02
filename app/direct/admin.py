"""Authenticated preview: no inbox, outbox, customer mutations or real sends."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.security import verify_admin_token
from app.direct.agent import DirectOpenAIAgent
from app.direct.knowledge import knowledge_documents, vector_store_for
from app.direct.tools import DirectTools
from app.models import IncomingMessage

router = APIRouter(tags=["direct-agent"], dependencies=[Depends(verify_admin_token)])


class PreviewRequest(BaseModel):
    workspace_id: UUID
    text: str = Field(min_length=1, max_length=8000)
    session: str | None = Field(default=None, max_length=150000)


def encode_session(payload, secret):
    raw = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    return raw + "." + hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


def decode_session(token, secret, workspace):
    try:
        raw, signature = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(raw))
        if payload["workspace"] != workspace or payload["expires"] < time.time():
            raise ValueError()
        return payload
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, detail="invalid_test_session") from None


async def preview_turn(payload):
    from app.persona.persona_runtime import load_persona_runtime, set_persona_runtime, reset_persona_runtime
    from app.configuration.runtime import bind_bundle, reset_bundle, settings_from_bundle
    from app.core.turn_cache import begin_turn_cache, end_turn_cache
    from app.llm.openai_client import get_async_openai_client
    cfg = get_settings()
    workspace = str(payload.workspace_id)
    state = (decode_session(payload.session, cfg.admin_api_token, workspace) if payload.session else
             {"workspace": workspace, "id": "direct-test:" + uuid4().hex, "history": [], "previous": {}})
    persona = await asyncio.to_thread(load_persona_runtime, workspace_id=workspace)
    if persona.workspace_id != workspace or persona.load_error or not persona.configuration_bundle:
        raise HTTPException(503, detail="workspace_configuration_unavailable")
    settings = settings_from_bundle(cfg, persona.configuration_bundle)
    cache = begin_turn_cache()
    bundle_token = bind_bundle(persona.configuration_bundle, settings)
    persona_token = set_persona_runtime(persona)
    try:
        incoming = IncomingMessage(provider="test", channel="whatsapp", sender_key=state["id"],
                                   conversation_id=state["id"], text=payload.text)
        docs = await asyncio.to_thread(knowledge_documents, persona)
        tools = DirectTools(incoming=incoming, history=state["history"], documents=docs)
        result = await DirectOpenAIAgent(get_async_openai_client(), settings).run_turn(
            incoming=incoming, workspace_id=workspace, history=state["history"], previous=state["previous"],
            tools=tools, content=[{"type": "input_text", "text": payload.text}],
            persona_name=persona.agent_display_name, tone=persona.tone or "natural",
            vector_store_id=vector_store_for(settings, workspace))
        state["history"] = (state["history"] + [{"role": "user", "content": payload.text},
                           {"role": "assistant", "content": result.reply_text}])[-20:]
        state["previous"] = result.response_metadata
        state["expires"] = time.time() + 3600
        return {"ok": True, "engine": "direct", "active_engine": cfg.nsagent_engine,
                "sent_to_customer": False, "reply_text": result.reply_text,
                "session": encode_session(state, cfg.admin_api_token),
                "metrics": result.response_metadata["direct_agent"]}
    finally:
        reset_persona_runtime(persona_token)
        reset_bundle(bundle_token)
        end_turn_cache(cache)


@router.post("/api/test/direct")
async def direct_preview(payload: PreviewRequest):
    try:
        return await preview_turn(payload)
    except HTTPException:
        raise
    except Exception as exc:
        from app.ops.observability import log_event
        log_event("direct.preview.failed", {"error_type": type(exc).__name__})
        raise HTTPException(503, detail="direct_preview_unavailable") from None


@router.post("/api/admin/direct/knowledge/{workspace_id}")
async def publish_knowledge(workspace_id: UUID):
    """Publish a new immutable knowledge snapshot. Activation is a separate env update."""
    from app.persona.persona_runtime import load_persona_runtime
    from app.configuration.runtime import bind_bundle, reset_bundle
    from app.llm.openai_client import get_async_openai_client
    persona = await asyncio.to_thread(load_persona_runtime, workspace_id=str(workspace_id))
    if persona.workspace_id != str(workspace_id) or persona.load_error or not persona.configuration_bundle:
        raise HTTPException(503, detail="workspace_configuration_unavailable")
    token = bind_bundle(persona.configuration_bundle, get_settings())
    try:
        docs = await asyncio.to_thread(knowledge_documents, persona)
    finally:
        reset_bundle(token)
    if not docs:
        raise HTTPException(409, detail="knowledge_empty")
    client = get_async_openai_client()
    content = "\n\n".join(f"# {d['title']}\nFonte: {d['source']}\n{d['text']}" for d in docs)
    digest = hashlib.sha256(content.encode()).hexdigest()
    uploaded = await client.files.create(file=("knowledge.md", content.encode(), "text/markdown"), purpose="assistants")
    store = await client.vector_stores.create(name=f"nsagent-{workspace_id}-{digest[:12]}",
                                              metadata={"workspace_id": str(workspace_id), "hash": digest})
    batch = await client.vector_stores.file_batches.create_and_poll(vector_store_id=store.id, file_ids=[uploaded.id])
    if batch.status != "completed" or batch.file_counts.failed:
        raise HTTPException(503, detail="knowledge_indexing_failed")
    return {"ok": True, "workspace_id": str(workspace_id), "vector_store_id": store.id,
            "hash": digest, "document_count": len(docs), "activated": False}
