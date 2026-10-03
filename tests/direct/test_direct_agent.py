from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import json

import pytest

from app.config import Settings
from app.direct.agent import DirectOpenAIAgent, session_scope
from app.direct.tools import DirectTools, tool_schemas
from app.models import IncomingMessage, AgentResult


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def incoming(text="Olá", **kwargs):
    return IncomingMessage(text=text, provider="meta", channel="instagram",
                           sender_key="ig:one", conversation_id="thread-one", **kwargs)


def response(text="Olá! Como posso ajudar?", calls=None, status="completed"):
    output = calls if calls else [NS(type="message", id="msg-final")]
    return NS(id="resp-1", status=status, output=output, output_text=text,
              usage=NS(input_tokens=100, output_tokens=20))


def client(responses):
    value = NS(responses=NS(create=AsyncMock(side_effect=responses)),
        conversations=NS(create=AsyncMock(return_value=NS(id="conv-1")),
                         items=NS(list=AsyncMock(return_value=NS(data=[NS(id="msg-final")])))))
    value.with_options = lambda **_: value
    return value


def test_knowledge_includes_published_persona_metadata(monkeypatch):
    from app.direct.knowledge import knowledge_documents, search_knowledge
    import app.persona.store_knowledge as store
    import app.persona.persona_knowledge_repository as repository
    monkeypatch.setattr(store, "_INSTITUTIONAL_SNIPPETS", lambda: [])
    monkeypatch.setattr(repository, "list_persona_attachments", lambda _: [])
    persona = NS(chatbo_persona_id=None, active_persona=NS(metadata={
        "institutionalKnowledge": [{"title": "Garantia", "content": "Garantia oficial de dois anos.",
                                    "source_url": "https://example.com/garantia"}]}))
    result = search_knowledge(knowledge_documents(persona), "garantia")
    assert result["documents"][0]["text"] == "Garantia oficial de dois anos."
    assert result["documents"][0]["source"] == "https://example.com/garantia"


@pytest.mark.asyncio
async def test_published_identity_reaches_direct_model():
    api = client([response()])
    message = incoming()
    await DirectOpenAIAgent(api, settings()).run_turn(
        incoming=message, workspace_id="workspace-one", history=[], previous={},
        tools=DirectTools(incoming=message, history=[], documents=[]),
        content=[{"type": "input_text", "text": "Oi"}], persona_name="Crono")
    prompt = api.responses.create.await_args.kwargs["instructions"]
    assert '"identidade": "Crono"' in prompt
    assert "apresente-se brevemente com esse nome" in prompt
    assert "use lookup_order" in prompt


async def run(api, message=None, previous=None, history=None, adapter=None, cfg=None):
    message = message or incoming()
    tools = DirectTools(incoming=message, history=history or [], documents=[], adapter=adapter)
    return await DirectOpenAIAgent(api, cfg or settings()).run_turn(incoming=message,
        workspace_id="workspace-one", history=history or [], previous=previous or {}, tools=tools,
        content=[{"type": "input_text", "text": message.text}])


@pytest.mark.asyncio
async def test_single_model_owns_reply_and_no_legacy_rewrite(monkeypatch):
    import app.verify.response_critique as critique
    monkeypatch.setattr(critique, "apply_response_critique_loop", AsyncMock(side_effect=AssertionError("legacy")))
    api = client([response("Entendi, você não quer o segundo.")])
    result = await run(api, incoming("não quero o segundo"))
    assert result.reply_text == "Entendi, você não quer o segundo."
    assert result.commercial_data is None
    assert api.responses.create.await_count == 1
    assert result.response_metadata["engine"] == "direct"


@pytest.mark.asyncio
async def test_tools_return_to_same_conversation():
    call = NS(type="function_call", id="fc-1", name="search_knowledge",
              call_id="call-1", arguments='{"query":"garantia"}')
    api = client([response(calls=[call]), response("Consultei a política.")])
    result = await run(api)
    requests = api.responses.create.await_args_list
    assert requests[0].kwargs["conversation"] == requests[1].kwargs["conversation"] == "conv-1"
    assert requests[1].kwargs["input"][0]["type"] == "function_call_output"
    assert requests[1].kwargs["input"][0]["call_id"] == "call-1"
    assert result.response_metadata["direct_agent"]["calls"] == 2


@pytest.mark.asyncio
async def test_only_delivered_tail_is_reused():
    api = client([response()])
    first = await run(api)
    api2 = client([response("Continuando")])
    await run(api2, previous=first.response_metadata)
    api2.conversations.create.assert_not_awaited()
    api2.conversations.items.list.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_undelivered_answer_causes_rebuild():
    api = client([response()])
    first = await run(api)
    api2 = client([response()])
    api2.conversations.items.list.return_value = NS(data=[NS(id="undelivered")])
    delivered = [{"role": "user", "content": "Olá"}, {"role": "assistant", "content": "Oi"}]
    await run(api2, previous=first.response_metadata, history=delivered)
    assert api2.conversations.create.await_args.kwargs["items"] == delivered


@pytest.mark.asyncio
async def test_different_identity_does_not_read_remote_session():
    api = client([response()])
    first = await run(api)
    changed = incoming().model_copy(update={"sender_key": "ig:another"})
    api2 = client([response()])
    await run(api2, message=changed, previous=first.response_metadata)
    api2.conversations.items.list.assert_not_awaited()
    api2.conversations.create.assert_awaited_once()


@pytest.mark.parametrize("field,value", [("channel", "whatsapp"), ("provider", "brevo"),
    ("conversation_id", "other"), ("sender_key", "another")])
def test_scope_separates_channels_and_contacts(field, value):
    msg = incoming()
    assert session_scope("w", msg) != session_scope("w", msg.model_copy(update={field: value}))
    assert session_scope("w", msg) != session_scope("other", msg)


@pytest.mark.asyncio
async def test_incomplete_is_not_sent():
    with pytest.raises(RuntimeError, match="incomplete"):
        await run(client([response("meia resposta", status="incomplete")]))


@pytest.mark.asyncio
async def test_empty_is_not_sent():
    with pytest.raises(RuntimeError, match="empty"):
        await run(client([response("")]))


@pytest.mark.asyncio
async def test_last_round_disables_tools():
    api = client([response()])
    await run(api, cfg=settings(DIRECT_MAX_ROUNDS=1))
    assert api.responses.create.await_args.kwargs["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_flag_branches_before_any_legacy_setup(monkeypatch):
    import app.message_pipeline as pipeline
    import app.direct.pipeline as direct
    sentinel = AgentResult(reply_text="direct")
    mock = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings(NSAGENT_ENGINE="direct"))
    monkeypatch.setattr(direct, "process_direct_message", mock)
    monkeypatch.setattr(pipeline, "_process_incoming_message", AsyncMock(side_effect=AssertionError("legacy")))
    assert await pipeline.process_incoming_message(incoming(), {}) is sentinel


def test_invalid_flag_is_rejected_and_default_is_legacy():
    from pydantic import ValidationError
    assert Settings.model_fields["nsagent_engine"].default == "legacy"
    with pytest.raises(ValidationError):
        settings(NSAGENT_ENGINE="typo")


def test_workspace_policies_cannot_override_engine():
    from app.configuration.runtime import settings_from_bundle
    cfg = settings_from_bundle(settings(), {"fields": [{"target": "setting", "attribute": "nsagent_engine", "key": "engine"}],
                                           "values": {"engine": "direct"}})
    assert cfg.nsagent_engine == "legacy"


def test_tool_surface_is_read_only_and_strict():
    names = {item["name"] for item in tool_schemas()}
    assert names == {"search_products", "search_ready_delivery", "get_product", "check_inventory", "search_knowledge", "request_human", "lookup_order", "prepare_product_image", "compare_ready_delivery_catalog", "get_ready_delivery_candidate", "get_ready_delivery_details", "remember_preference", "update_conversation_context", "read_knowledge_document", "find_story_reference", "list_product_variants", "quote_product_shipping"}
    for item in tool_schemas():
        assert item["strict"] and item["parameters"]["additionalProperties"] is False
        assert set(item["parameters"]["required"]) == set(item["parameters"]["properties"])


@pytest.mark.asyncio
@pytest.mark.parametrize("name,raw", [("create_cart", "{}"), ("get_product", '{"product_id":"../../orders"}'),
    ("search_products", '{"query":"Seiko","workspace_id":"other"}')])
async def test_untrusted_tools_do_not_reach_adapter(name, raw):
    adapter = NS(search_products=AsyncMock(), get_product=AsyncMock())
    tools = DirectTools(incoming=incoming(), history=[], documents=[], adapter=adapter)
    assert (await tools.execute(name, raw))["ok"] is False
    adapter.search_products.assert_not_awaited()
    adapter.get_product.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_handoff_without_consent():
    tools = DirectTools(incoming=incoming("não quero atendimento humano"), history=[], documents=[])
    result = await tools.execute("request_human", '{"reason":"pedido do modelo"}')
    assert result["ok"] is False and tools.handoff is None


@pytest.mark.asyncio
async def test_unconfirmed_transfer_promise_becomes_an_offer():
    from app.ops.handoff_consent import offer_text
    result = await run(client([response(
        "Posso te orientar, mas não consultei o pedido. Vou te passar para o João, da equipe.")]),
        incoming("Prazo do meu pedido efetuado"))
    assert result.handoff_required is False
    assert result.response_metadata["handoff"]["offer"] is True
    assert result.response_metadata["handoff"]["confirmed"] is False
    assert "João" not in result.reply_text
    assert offer_text() in result.reply_text


@pytest.mark.asyncio
async def test_explicit_human_request_is_queued_even_if_the_model_skips_the_tool():
    from app.configuration.runtime import message
    result = await run(client([response("Vou te passar para o João da equipe.")]),
                       incoming("Quero falar com um atendente"))
    assert result.handoff_required is True
    assert result.reply_text == message("handoff_requested")
    assert result.response_metadata["handoff"]["consent_reason"] == "customer_requested_human"
    assert result.response_metadata["handoff"]["confirmed"] is True


@pytest.mark.asyncio
async def test_acceptance_of_the_previous_offer_confirms_handoff():
    from app.ops.handoff_consent import offer_text
    from app.configuration.runtime import message
    history = [{"role": "assistant", "content": offer_text(),
                "metadata": {"handoff": {"offer": True, "required": False}}}]
    result = await run(client([response("Certo, sigo por aqui.")]), incoming("sim"), history=history)
    assert result.handoff_required is True
    assert result.reply_text == message("handoff_requested")
    assert result.response_metadata["handoff"]["consent_reason"] == "customer_accepted_handoff_offer"


@pytest.mark.asyncio
async def test_bare_yes_does_not_confirm_handoff():
    result = await run(client([response("Como posso ajudar?")]), incoming("sim"))
    assert result.handoff_required is False
    assert "handoff" not in result.response_metadata


@pytest.mark.asyncio
async def test_adapter_failure_is_not_empty_stock():
    from app.tray.tray_adapter_client import TrayAdapterError
    adapter = NS(get_product=AsyncMock(side_effect=TrayAdapterError("secret internal error")))
    tools = DirectTools(incoming=incoming(), history=[], documents=[], adapter=adapter)
    result = await tools.execute("get_product", '{"product_id":"2"}')
    assert result["error"] == "commerce_unavailable" and "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_search_constraints_and_public_fields():
    adapter = NS(search_products=AsyncMock(return_value={"products": [{"id": "1", "name": "Seiko", "access_token": "SECRET"}]}))
    tools = DirectTools(incoming=incoming(), history=[], documents=[], adapter=adapter)
    result = await tools.execute("search_products", '{"query":"Seiko","max_price":3000,"ready_stock":false}')
    args = adapter.search_products.await_args.kwargs
    assert args["available_in_store"] is None and args["current_price_range"] == "0,3000"
    assert args["limit"] == 5
    assert "SECRET" not in json.dumps(result)


@pytest.mark.asyncio
async def test_empty_ready_delivery_asks_for_made_to_order_catalog():
    adapter = NS(search_ready_delivery=AsyncMock(return_value={
        "query": "Orient open heart", "total": 0, "returned": 0, "products": []}))
    tools = DirectTools(incoming=incoming(), history=[], documents=[], adapter=adapter)
    result = await tools.execute("search_ready_delivery",
                                 '{"query":"Orient open heart","offset":0,"limit":10}')
    assert "search_products" in result["instruction"]
    assert "sob encomenda" in result["instruction"]


@pytest.mark.asyncio
async def test_catalog_upon_request_is_labeled_made_to_order():
    adapter = NS(search_products=AsyncMock(return_value={"products": [{
        "id": "9", "name": "Orient Open Heart", "url": "https://www.newstorerj.com/orient",
        "upon_request": True}]}))
    tools = DirectTools(incoming=incoming(), history=[], documents=[], adapter=adapter)
    result = await tools.execute("search_products",
                                 '{"query":"Orient open heart","ready_stock":false}')
    assert result["data"]["products"][0]["upon_request"] is True
    assert "sob encomenda" in result["instruction"]
    assert adapter.search_products.await_args.kwargs["available_in_store"] is None


def test_preview_session_tampering_expiry_and_workspace():
    from app.direct.admin import encode_session, decode_session
    from fastapi import HTTPException
    token = encode_session({"workspace": "a", "expires": 99999999999}, "secret")
    assert decode_session(token, "secret", "a")["workspace"] == "a"
    for candidate, workspace in [(token + "x", "a"), (token, "b"),
        (encode_session({"workspace": "a", "expires": 0}, "secret"), "a")]:
        with pytest.raises(HTTPException):
            decode_session(candidate, "secret", workspace)


def test_vector_store_requires_exact_workspace():
    from app.direct.knowledge import vector_store_for
    cfg = settings(DIRECT_VECTOR_STORES='{"a":"vs_one"}')
    assert vector_store_for(cfg, "a") == "vs_one"
    assert vector_store_for(cfg, "b") is None
