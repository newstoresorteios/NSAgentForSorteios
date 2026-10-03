"""Prepare trusted inputs and call the direct agent; no legacy semantic pipeline."""
from __future__ import annotations

import asyncio

from app.config import get_settings
from app.models import AgentResult
from app.direct.agent import DirectOpenAIAgent
from app.direct.history import load_history
from app.direct.knowledge import knowledge_documents, vector_store_for
from app.direct.tools import DirectTools
from app.direct.persona import persona_context


async def input_content(incoming):
    from app.direct.media import visual_content
    from app.channels.audio_service import transcribe_audio_url
    text = incoming.text or ""
    if incoming.audio_url:
        try:
            text = await transcribe_audio_url(incoming.audio_url, filename=incoming.audio_filename)
            incoming.text = text
        except Exception:
            text += "\n[Áudio indisponível: peça ao cliente para escrever ou reenviar.]"
    content = [{"type": "input_text", "text": text or "[Mídia recebida]"}]
    content.extend(await visual_content(incoming))
    return content


async def process_direct_message(incoming, customer_context):
    from app.configuration.workspace import resolve_message_workspace, resolve_ingress_workspace, _incoming_account_ref, stamp_inbound_workspace
    from app.persona.persona_runtime import load_persona_runtime, set_persona_runtime, reset_persona_runtime
    from app.configuration.runtime import bind_bundle, reset_bundle, settings_from_bundle
    from app.core.turn_cache import begin_turn_cache, end_turn_cache
    from app.llm.openai_client import get_async_openai_client
    from app.ops.observability import log_event
    base = get_settings()
    cache = begin_turn_cache()
    persona_token = bundle_token = None
    try:
        workspace = await asyncio.to_thread(resolve_message_workspace, incoming)
        if not workspace:
            workspace = await asyncio.to_thread(resolve_ingress_workspace,
                incoming.conversation_id, incoming.channel, _incoming_account_ref(incoming))
        if not workspace:
            raise RuntimeError("direct_workspace_required")
        persona = await asyncio.to_thread(load_persona_runtime, workspace_id=workspace)
        if persona.load_error or not persona.configuration_bundle:
            raise RuntimeError("direct_configuration_unavailable")
        if persona.workspace_id != workspace:
            raise RuntimeError("direct_workspace_mismatch")
        settings = settings_from_bundle(base, persona.configuration_bundle)
        bundle_token = bind_bundle(persona.configuration_bundle, settings)
        persona_token = set_persona_runtime(persona)
        await asyncio.to_thread(stamp_inbound_workspace, incoming.raw.get("inbound_id"), workspace)
        history, previous = await asyncio.to_thread(load_history, incoming, workspace)
        documents = await asyncio.to_thread(knowledge_documents, persona)
        from app.direct.continuity import learned_context
        learned = await asyncio.to_thread(learned_context, persona, incoming)
        memories = ""
        if incoming.sender_key:
            from app.memory.contact_memory_repository import select_relevant_memories, format_customer_memory_block
            try:
                selected = await asyncio.to_thread(select_relevant_memories, tenant_id=persona.tenant_id,
                    workspace_id=workspace, sender_key=incoming.sender_key, limit=8, max_chars=1500)
                memories = format_customer_memory_block(selected)
            except Exception as exc:
                log_event("direct.memory.unavailable", {"error_type": type(exc).__name__})
        tools = DirectTools(incoming=incoming, history=history, documents=documents,
                            workspace=workspace, tenant=persona.tenant_id, learned=learned,
                            continuity=(previous.get('direct_agent') or {}).get('continuity'),
                            products=(previous.get('direct_agent') or {}).get('products', []),
                            catalog_snapshot=(previous.get('direct_agent') or {}).get('catalog_snapshot'))
        result = await DirectOpenAIAgent(get_async_openai_client(), settings).run_turn(
            incoming=incoming, workspace_id=workspace, history=history, previous=previous,
            tools=tools, content=await input_content(incoming), memories=memories,
            persona_name=persona.agent_display_name, tone=persona.tone or "natural",
            vector_store_id=vector_store_for(settings, workspace), persona=persona_context(persona))
        result.response_metadata["persona_runtime"] = {"workspace_id": workspace,
            "persona_version_id": persona.persona_version_id}
        log_event("direct.turn.completed", {k: v for k, v in result.response_metadata["direct_agent"].items()
                  if k in {"model", "calls", "tools", "input_tokens", "output_tokens", "latency_ms",
                           "prompt_version", "persona_sha256", "knowledge_document_count", "catalog_searches"}})
        return result
    except Exception as exc:
        # Do not serialize exception messages: API errors can contain request data.
        from app.direct.diagnostics import safe_error_details
        log_event("direct.turn.failed", safe_error_details(exc))
        metadata = {"engine": "direct", "response_source": "direct_unavailable"}
        if persona_token is not None:
            metadata["persona_runtime"] = {"workspace_id": workspace}
        return AgentResult(reply_text="Não consegui concluir sua consulta agora. Pode tentar novamente em instantes?",
            safety_reason="direct_unavailable", response_metadata=metadata)
    finally:
        if persona_token is not None:
            reset_persona_runtime(persona_token)
        if bundle_token is not None:
            reset_bundle(bundle_token)
        end_turn_cache(cache)
