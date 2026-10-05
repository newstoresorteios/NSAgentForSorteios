import asyncio
import copy
import json
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.direct.checkout import (CheckoutItem, carry_checkout_context, handle_checkout_command,
    prepare_checkout, review_result)
from app.direct.tools import DirectTools, tool_schemas
from app.models import AgentResult, IncomingMessage
from app.tray.tray_adapter_client import TrayAdapterClient
from app.tray.tray_circuit_breaker import reset_tray_circuit_breaker_for_tests

WORKSPACE = "00000000-0000-0000-0000-000000000001"
PRODUCT = {"id": "42", "name": "Relógio Seiko", "available": True, "stock": 3, "price": "1250.00"}


def incoming(text="quero comprar", **changes):
    values = dict(text=text, provider="brevo", channel="whatsapp", sender_key="customer:one",
                  conversation_id="thread-one", message_id="msg-one")
    values.update(changes)
    return IncomingMessage(**values)


def adapter():
    return NS(_request=AsyncMock(return_value={"success": True, "product": dict(PRODUCT)}),
              get_product_variant=AsyncMock(), create_cart=AsyncMock(), get_cart_complete=AsyncMock())


class Journal:
    """Offline receipt store implementing the same atomic claim contract."""
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    def claim(self, proposal):
        with self.lock:
            key = proposal["id"]
            owner = key not in self.rows
            if owner:
                self.rows[key] = {"status": "started", "result": {}, "proposal": copy.deepcopy(proposal)}
            assert self.rows[key]["proposal"] == proposal
            return owner, copy.deepcopy(self.rows[key])

    def finish(self, proposal, status, result):
        with self.lock:
            row = self.rows[proposal["id"]]
            assert row["status"] == "started"
            row.update(status=status, result=copy.deepcopy(result))


async def proposal(api=None, **item):
    api = api or adapter()
    result, value = await prepare_checkout(adapter=api, item=CheckoutItem(product_id="42", **item),
        incoming=incoming(), workspace=WORKSPACE, known_ids={"42"})
    assert result["mutated"] is False
    api.create_cart.assert_not_awaited()
    return value


def setup_success(api, p):
    api.create_cart.return_value = {"success": True, "cart": {"session_id": p["id"],
        "cart_url": "https://store.test/cart/official"}}
    api.get_cart_complete.return_value = {"success": True, "cart": {"session_id": p["id"], "items": [{
        "product_id": p["product_id"], "variant_id": p["variant_id"],
        "quantity": p["quantity"], "price": p["unit_price"]}]}}


async def confirm(p, api, journal, **kwargs):
    message = kwargs.pop("message", incoming("CONFIRMAR " + p["id"][:8].upper()))
    previous = kwargs.pop("previous", review_result(p).response_metadata)
    return await handle_checkout_command(message, previous, kwargs.pop("workspace", WORKSPACE),
        enabled=kwargs.pop("enabled", True), adapter=api, journal=journal, **kwargs)


@pytest.mark.asyncio
async def test_review_uses_server_price_quantity_and_no_mutation():
    p = await proposal(quantity=2)
    result = review_result(p)
    assert "2 × Relógio Seiko" in result.reply_text
    assert "1250,00" in result.reply_text and "2500" in result.reply_text
    assert p["id"][:8].upper() in result.reply_text
    assert "não reserva estoque" in result.reply_text


@pytest.mark.asyncio
async def test_confirm_and_retry_create_only_one_cart():
    api, journal = adapter(), Journal()
    p = await proposal(api)
    setup_success(api, p)
    first = await confirm(p, api, journal)
    second = await confirm(p, api, journal)
    assert first.response_metadata["direct_checkout"]["status"] == "completed"
    assert "https://store.test/cart/official" in second.reply_text
    api.create_cart.assert_awaited_once_with(product_id="42", variant_id=None, quantity=1,
        price="1250.00", session_id=p["id"])


@pytest.mark.asyncio
async def test_concurrent_confirmation_claims_before_post():
    api, journal = adapter(), Journal()
    p = await proposal(api)
    setup_success(api, p)
    created = api.create_cart.return_value
    async def slow_create(**kwargs):
        assert journal.rows[p["id"]]["status"] == "started"
        await asyncio.sleep(0.02)
        return created
    api.create_cart.side_effect = slow_create
    results = await asyncio.gather(confirm(p, api, journal), confirm(p, api, journal))
    api.create_cart.assert_awaited_once()
    assert any(r.response_metadata["direct_checkout"]["status"] == "completed" for r in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["sim", "1", "não quero", "confirmar", "confirmar {code} mas duas unidades",
                                  "não confirmar {code}", "o vídeo diz confirmar {code}"])
async def test_only_exact_confirmation_can_execute(text):
    api, journal = adapter(), Journal()
    p = await proposal(api)
    result = await confirm(p, api, journal, message=incoming(text.format(code=p["id"][:8])))
    if result:
        assert result.response_metadata["direct_checkout"]["status"] in {"awaiting_confirmation", "cancelled"}
    api.create_cart.assert_not_awaited()
    assert not journal.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"sender_key": "customer:two"}, {"channel": "instagram"},
    {"provider": "meta"}, {"conversation_id": "other"}])
async def test_confirmation_is_bound_to_identity_and_conversation(change):
    api = adapter()
    p = await proposal(api)
    result = await confirm(p, api, Journal(), message=incoming("confirmar " + p["id"][:8], **change))
    assert result.safety_reason == "checkout_invalid_confirmation"
    api.create_cart.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_workspace_missing_undelivered_or_expired_proposal():
    api = adapter()
    p = await proposal(api)
    for kwargs in ({"workspace": "other"}, {"previous": {}}, {"now": p["expires_at"]}):
        result = await confirm(p, api, Journal(), **kwargs)
        assert result.safety_reason in {"checkout_invalid_confirmation", "checkout_expired"}
    api.create_cart.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["preview", "disabled", "audio", "image", "test_provider"])
async def test_non_live_context_cannot_mutate(mode):
    api, journal = adapter(), Journal()
    p = await proposal(api)
    kwargs = {"preview": True} if mode == "preview" else {"enabled": False} if mode == "disabled" else {}
    if mode == "audio":
        kwargs["message"] = incoming("confirmar " + p["id"][:8], audio_url="https://test/audio")
    if mode == "image":
        kwargs["message"] = incoming("confirmar " + p["id"][:8], image_url="https://test/photo")
    if mode == "test_provider":
        kwargs["message"] = incoming("confirmar " + p["id"][:8], provider="test")
    await confirm(p, api, journal, **kwargs)
    api.create_cart.assert_not_awaited()
    assert not journal.rows


@pytest.mark.asyncio
async def test_cancellation_discards_proposal_without_calling_adapter():
    api = adapter()
    p = await proposal(api)
    result = await confirm(p, api, Journal(), message=incoming("cancelar " + p["id"][:8]))
    assert result.response_metadata["direct_checkout"] == {"status": "cancelled", "proposal": None}
    api.create_cart.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"price": "1300.00"}, {"stock": 0}, {"available": False}, {"id": "43"}])
async def test_changed_facts_require_new_confirmation(change):
    api, journal = adapter(), Journal()
    p = await proposal(api)
    api._request.return_value["product"].update(change)
    result = await confirm(p, api, journal)
    assert result.safety_reason == "checkout_revalidation_required"
    assert journal.rows[p["id"]]["status"] == "rejected"
    api.create_cart.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_does_not_retry_post_and_never_claims_payment():
    api, journal = adapter(), Journal()
    p = await proposal(api)
    api.create_cart.side_effect = TimeoutError("secret upstream")
    for _ in range(2):
        result = await confirm(p, api, journal)
        assert result.safety_reason == "checkout_reconciliation_required"
        assert "secret" not in result.reply_text
    api.create_cart.assert_awaited_once()
    assert journal.rows[p["id"]]["status"] == "unknown"


@pytest.mark.asyncio
async def test_process_interruption_keeps_started_claim():
    api, journal = adapter(), Journal()
    p = await proposal(api)
    api.create_cart.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await confirm(p, api, journal)
    assert journal.rows[p["id"]]["status"] == "started"
    result = await confirm(p, api, journal)
    assert result.safety_reason == "checkout_reconciliation_required"
    api.create_cart.assert_awaited_once()


@pytest.mark.asyncio
async def test_journal_outage_fails_before_post():
    api = adapter()
    p = await proposal(api)
    journal = NS(claim=lambda _: (_ for _ in ()).throw(RuntimeError("database secret")))
    result = await confirm(p, api, journal)
    assert result.safety_reason == "checkout_journal_unavailable"
    api.create_cart.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"price": "1.00"}, {"quantity": 2}, {"product_id": "43"}, {"variant_id": "9"}])
async def test_created_cart_must_match_review(change):
    api, journal = adapter(), Journal()
    p = await proposal(api)
    setup_success(api, p)
    api.get_cart_complete.return_value["cart"]["items"][0].update(change)
    result = await confirm(p, api, journal)
    assert result.safety_reason == "checkout_reconciliation_required"
    assert "https://" not in result.reply_text


@pytest.mark.asyncio
async def test_variation_requires_identity_price_stock_and_label():
    api = adapter()
    api._request.return_value["product"]["has_variation"] = "1"
    with pytest.raises(ValueError, match="variant_required"):
        await proposal(api)
    api.get_product_variant.return_value = {"variant": {"id": "99", "product_id": "42",
        "price": "1500.00", "stock": 2, "sku": [{"type": "Cor", "value": "Azul"}]}}
    p = await proposal(api, variant_id="99")
    assert p["unit_price"] == "1500.00" and p["variant_label"] == "Azul"
    api.get_product_variant.return_value["variant"]["product_id"] = "wrong"
    with pytest.raises(ValueError, match="variant_identity_mismatch"):
        await proposal(api, variant_id="99")


@pytest.mark.asyncio
async def test_public_listing_cannot_prepare_admin_checkout():
    api = adapter()
    result, p = await prepare_checkout(adapter=api, item=CheckoutItem(product_id="42"),
        incoming=incoming(), workspace=WORKSPACE, known_ids=set())
    assert p is None and result["error"] == "known_admin_product_required"
    api._request.assert_not_awaited()


def test_model_has_proposal_only_and_cannot_enable_feature():
    assert "prepare_checkout" not in {t["name"] for t in tool_schemas()}
    names = {t["name"] for t in tool_schemas(checkout_enabled=True)}
    assert "prepare_checkout" in names
    assert not names & {"create_cart", "create_order", "cancel_order", "execute_checkout", "confirm_checkout"}
    assert Settings.model_fields["direct_checkout_enabled"].default is False
    from app.configuration.runtime import settings_from_bundle
    cfg = settings_from_bundle(Settings(_env_file=None), {"fields": [{"target": "setting",
        "attribute": "direct_checkout_enabled", "key": "enabled"}], "values": {"enabled": True}})
    assert cfg.direct_checkout_enabled is False


@pytest.mark.asyncio
async def test_tool_loop_stops_at_review_without_second_model_call():
    from app.direct.agent import DirectOpenAIAgent
    from tests.direct.test_direct_agent import client, response
    call = NS(type="function_call", id="fc", name="prepare_checkout", call_id="call",
              arguments=json.dumps({"product_id": "42", "variant_id": None, "quantity": 1}))
    api = client([response(calls=[call])])
    gateway = adapter()
    tools = DirectTools(incoming=incoming(), history=[], documents=[], adapter=gateway,
                        workspace=WORKSPACE, checkout_enabled=True)
    tools.admin_product_ids.add("42")
    result = await DirectOpenAIAgent(api, Settings(_env_file=None)).run_turn(incoming=incoming(),
        workspace_id=WORKSPACE, history=[], previous={}, tools=tools, content=[{"type": "input_text", "text": "comprar"}])
    assert result.response_metadata["response_source"] == "direct_checkout"
    assert result.response_metadata["direct_agent"]["calls"] == 1
    api.responses.create.assert_awaited_once()
    gateway.create_cart.assert_not_awaited()
    assert "last_item_id" not in result.response_metadata["direct_agent"]


def test_deterministic_reply_keeps_products_but_resets_remote_tail():
    previous = {"direct_agent": {"products": [{"id": "42"}], "last_item_id": "old",
                "conversation_id": "remote"}, "direct_checkout": {"status": "awaiting_confirmation"}}
    reply = AgentResult(reply_text="ok", response_metadata={"response_source": "direct_checkout", "direct_agent": {}})
    carry_checkout_context(reply, previous)
    assert reply.response_metadata["direct_agent"] == {"products": [{"id": "42"}]}
    assert reply.response_metadata["direct_checkout"] == previous["direct_checkout"]


@pytest.mark.asyncio
async def test_outbox_retry_preserves_delivered_confirmation_and_workspace():
    from app.ingress.outbox import build_outbound_envelope, result_from_outbox_row
    api, journal = adapter(), Journal()
    p = await proposal(api)
    setup_success(api, p)
    reviewed = review_result(p)
    reviewed.response_metadata["persona_runtime"] = {"workspace_id": WORKSPACE}
    reviewed.response_metadata["direct_agent"].update({"products": [{"id": "42"}],
        "conversation_id": "unverified-remote-tail", "instructions": "MUST NOT COPY"})
    row = {"reply_text": reviewed.reply_text, "reply_payload": build_outbound_envelope(incoming(), reviewed)}
    delivered = result_from_outbox_row(row)
    assert delivered.response_metadata["persona_runtime"]["workspace_id"] == WORKSPACE
    assert delivered.response_metadata["direct_checkout"]["proposal"] == p
    assert "MUST NOT COPY" not in json.dumps(row)
    assert "conversation_id" not in delivered.response_metadata["direct_agent"]
    confirmed = await confirm(p, api, journal, previous=delivered.response_metadata)
    assert confirmed.response_metadata["direct_checkout"]["status"] == "completed"


@pytest.mark.asyncio
async def test_pipeline_executes_confirmation_without_openai_or_legacy(monkeypatch):
    import app.direct.pipeline as pipeline
    import app.configuration.workspace as workspace_module
    import app.configuration.runtime as runtime
    import app.persona.persona_runtime as persona_module
    import app.llm.openai_client as openai_module
    import app.tray.tray_adapter_client as adapter_module
    import app.direct.checkout_journal as journal_module
    api, journal = adapter(), Journal()
    p = await proposal(api)
    setup_success(api, p)
    settings = Settings(_env_file=None, DIRECT_CHECKOUT_ENABLED=True)
    persona = NS(load_error=None, configuration_bundle={"values": {"test": True}},
                 workspace_id=WORKSPACE, persona_version_id="version")
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    monkeypatch.setattr(workspace_module, "resolve_message_workspace", lambda _: WORKSPACE)
    monkeypatch.setattr(workspace_module, "stamp_inbound_workspace", lambda *_: None)
    monkeypatch.setattr(persona_module, "load_persona_runtime", lambda **_: persona)
    monkeypatch.setattr(persona_module, "set_persona_runtime", lambda _: None)
    monkeypatch.setattr(runtime, "settings_from_bundle", lambda *_: settings)
    monkeypatch.setattr(runtime, "bind_bundle", lambda *_: None)
    monkeypatch.setattr(pipeline, "load_history", lambda *_: ([], review_result(p).response_metadata))
    def forbidden():
        raise AssertionError("OpenAI must not run for confirmation")
    monkeypatch.setattr(openai_module, "get_async_openai_client", forbidden)
    monkeypatch.setattr(adapter_module, "TrayAdapterClient", lambda: api)
    monkeypatch.setattr(journal_module, "CheckoutJournal", lambda: journal)
    result = await pipeline.process_direct_message(incoming("confirmar " + p["id"][:8]), {})
    assert result.response_metadata["direct_checkout"]["status"] == "completed"
    assert result.response_metadata["persona_runtime"]["workspace_id"] == WORKSPACE
    api.create_cart.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", [None, "99"])
async def test_checkout_internal_http_contract(variant):
    reset_tray_circuit_breaker_for_tests()
    seen = []
    session = None
    def respond(request):
        nonlocal session
        seen.append(request)
        if request.url.path == "/internal/products/42":
            return httpx.Response(200, json={"success": True, "product": PRODUCT})
        if request.url.path == "/internal/products/variants/99":
            return httpx.Response(200, json={"success": True, "variant": {
                "id": "99", "product_id": "42", "stock": 3, "price": "1250.00", "reference": "AZUL"}})
        if request.url.path == "/internal/carts":
            body = json.loads(request.content)
            session = body["session_id"]
            expected = {"product_id": "42", "quantity": 1, "price": "1250.00", "session_id": session}
            if variant:
                expected["variant_id"] = variant
            assert body == expected
            return httpx.Response(200, json={"success": True, "cart": {"session_id": session,
                "cart_url": "https://store.test/cart/official"}})
        assert request.url.path == f"/internal/carts/{session}/complete"
        return httpx.Response(200, json={"success": True, "cart": {"items": [{
            "product_id": "42", "variant_id": variant, "quantity": 1, "price": "1250.00"}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=12) as http:
        gateway = TrayAdapterClient("https://adapter.test", "private-test-token", http)
        _, p = await prepare_checkout(adapter=gateway, item=CheckoutItem(product_id="42", variant_id=variant),
            incoming=incoming(), workspace=WORKSPACE, known_ids={"42"})
        result = await confirm(p, gateway, Journal())
    assert result.response_metadata["direct_checkout"]["status"] == "completed"
    assert [r.method for r in seen] == (["GET"] * (4 if variant else 2) + ["POST", "GET"])
    for r in seen:
        assert r.headers["authorization"] == "Bearer private-test-token"
        assert not r.url.query
        assert r.extensions["timeout"]["read"] == 12
        if r.method == "GET":
            assert not r.content
    reset_tray_circuit_breaker_for_tests()


@pytest.mark.asyncio
async def test_receipt_write_failure_never_repeats_external_mutation():
    api, journal = adapter(), Journal()
    p = await proposal(api)
    setup_success(api, p)
    def fail_write(*args):
        raise RuntimeError("storage unavailable")
    journal.finish = fail_write
    first = await confirm(p, api, journal)
    assert first.safety_reason == "checkout_journal_unavailable"
    second = await confirm(p, api, journal)
    assert second.safety_reason == "checkout_reconciliation_required"
    api.create_cart.assert_awaited_once()


@pytest.mark.asyncio
async def test_unsuccessful_cart_read_never_delivers_link():
    api, journal = adapter(), Journal()
    p = await proposal(api)
    setup_success(api, p)
    api.get_cart_complete.return_value["success"] = False
    result = await confirm(p, api, journal)
    assert result.safety_reason == "checkout_reconciliation_required"
    assert "https://" not in result.reply_text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 422, 503, "timeout"])
async def test_cart_post_transport_failure_is_never_retried(failure):
    reset_tray_circuit_breaker_for_tests()
    posts = []
    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "product": PRODUCT})
        posts.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret", request=request)
        return httpx.Response(failure, json={"error": "secret"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=12) as http:
        gateway = TrayAdapterClient("https://adapter.test", "test-token", http)
        _, p = await prepare_checkout(adapter=gateway, item=CheckoutItem(product_id="42"),
            incoming=incoming(), workspace=WORKSPACE, known_ids={"42"})
        journal = Journal()
        for _ in range(2):
            result = await confirm(p, gateway, journal)
            assert result.safety_reason == "checkout_reconciliation_required"
            assert "secret" not in result.reply_text
    assert len(posts) == 1
    reset_tray_circuit_breaker_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 404, 503, "timeout"])
async def test_review_transport_error_is_normalized(failure):
    reset_tray_circuit_breaker_for_tests()
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("secret upstream", request=request)
        return httpx.Response(failure, json={"error": "secret upstream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        gateway = TrayAdapterClient("https://adapter.test", "test-token", http)
        gateway.max_get_attempts = 1
        tools = DirectTools(incoming=incoming(), history=[], documents=[], adapter=gateway,
                            workspace=WORKSPACE, checkout_enabled=True)
        tools.admin_product_ids.add("42")
        result = await tools.execute("prepare_checkout", '{"product_id":"42","quantity":1}')
    assert result["error"] == "checkout_review_unavailable"
    assert "secret" not in json.dumps(result)
    assert tools.checkout_proposal is None
    reset_tray_circuit_breaker_for_tests()
