from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.checkout_data_service import update_checkout_data
from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.order_service import (
    confirm_prepared_order,
    create_order,
    get_order_facts,
    prepare_order,
)
from app.shipping_service import quote_shipping, select_shipping
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _cart(*, variant=True, multiple=False):
    items = [{
        "product_id": "803",
        "variant_id": "V1" if variant else None,
        "quantity": 1,
        "unit_price": "4699.99",
        "name": "Relogio",
    }]
    if multiple:
        items.append({
            "product_id": "804",
            "variant_id": None,
            "quantity": 2,
            "price": "100.00",
            "name": "Acessorio",
        })
    return {"items": items, "subtotal": "4899.99" if multiple else "4699.99"}


def _whatsapp_state(**overrides):
    payload = {
        "active_domain": "commerce",
        "checkout_channel_preference": "whatsapp",
        "cart_session_id": "SESSION-1",
        "cart_url": "https://loja.example/checkout/SESSION-1",
        "cart_items": [{"product_id": "803", "variant_id": "V1", "quantity": 1}],
    }
    payload.update(overrides)
    return CommerceConversationState(**payload)


def _ready_state(**overrides):
    payload = {
        **_whatsapp_state().model_dump(mode="json"),
        "shipping_quote_zipcode": "19900000",
        "shipping_quotes": [{
            "shipping_id": "1",
            "quotation_id": "Q1",
            "name": "PAC Tray",
            "price": "35.10",
            "min_period": 3,
            "max_period": 8,
        }],
        "selected_shipping": {
            "shipping_id": "1",
            "quotation_id": "Q1",
            "name": "PAC Tray",
            "price": "35.10",
            "min_period": 3,
            "max_period": 8,
        },
        "selected_payment_method": "pix",
        "selected_payment_option_id": "10545",
        "selected_payment_option": {
            "id": "10545", "name": "Pix - Vindi", "method": "pix",
        },
        "checkout_draft": {
            "customer": {
                "type": "0", "name": "Joao Pedro", "cpf": "52998224725",
                "email": "joao@example.com", "phone": "5511999999999",
            },
            "address": {
                "address": "Rua Um", "zip_code": "19900000", "number": "10",
                "neighborhood": "Centro", "city": "Ourinhos", "state": "SP",
                "country": "BRA", "type": "1",
            },
        },
    }
    payload.update(overrides)
    return CommerceConversationState(**payload)


def _interpretation(**overrides):
    payload = {
        "domain": "commerce",
        "goal": "buy",
        "subject": {},
        "preferences": {},
        "information_needed": [],
        "references_previous_context": True,
        "needs_clarification": False,
        "confidence": 0.99,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("variant,multiple", [(True, False), (False, False), (True, True)])
async def test_shipping_quote_uses_only_complete_cart_products(variant, multiple):
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return _cart(variant=variant, multiple=multiple)
        if tool == "quote_shipping":
            return {
                "success": True,
                "zipcode": "19900000",
                "options": [{
                    "shipping_id": 1, "quotation_id": "Q1", "name": "PAC Tray",
                    "price": "35.10", "min_period": 3, "max_period": 8,
                }],
            }
        raise AssertionError(tool)

    result = await quote_shipping(
        state=_whatsapp_state(), zipcode="19900-000", execute=execute,
    )

    assert result.safety_reason is None
    posted = calls[1][1]
    assert posted["zipcode"] == "19900000"
    assert len(posted["products"]) == (2 if multiple else 1)
    assert posted["products"][0] == {
        "product_id": "803",
        "variant_id": "V1" if variant else None,
        "price": "4699.99",
        "quantity": 1,
    }
    assert result.commercial_data["options"][0]["price"] == "35.10"


@pytest.mark.asyncio
async def test_shipping_rejects_invalid_zipcode_adapter_error_and_empty_options():
    async def never(*_args):
        raise AssertionError("invalid zipcode must not call adapter")

    invalid = await quote_shipping(state=_whatsapp_state(), zipcode="123", execute=never)
    assert invalid.safety_reason == "shipping_zipcode_invalid"

    async def failing(tool, _arguments):
        if tool == "get_cart_complete":
            return _cart()
        return {"error": "commerce_upstream_error", "status_code": 400}

    failed = await quote_shipping(state=_whatsapp_state(), zipcode="19900000", execute=failing)
    assert failed.commercial_data == {
        "success": False, "stage": "shipping_quote", "recoverable": False,
    }

    async def empty(tool, _arguments):
        return _cart() if tool == "get_cart_complete" else {"success": True, "options": []}

    no_options = await quote_shipping(state=_whatsapp_state(), zipcode="19900000", execute=empty)
    assert no_options.safety_reason == "shipping_options_empty"
    assert no_options.commercial_data["options"] == []


def test_shipping_selection_accepts_only_active_quote_and_never_free_price():
    state = _ready_state(selected_shipping=None)
    selected = select_shipping(state, selection_position=1)
    updated = evolve_commerce_state(state, selected)

    assert selected.safety_reason is None
    assert updated.selected_shipping is not None
    assert updated.selected_shipping.price == "35.10"
    rejected = select_shipping(state, selection_id="999")
    assert rejected.safety_reason == "shipping_selection_invalid"


def test_checkout_draft_partial_updates_and_missing_fields_are_persistent():
    state = _whatsapp_state()
    first = update_checkout_data(state, {"name": "Joao", "cpf": "529.982.247-25"})
    state = evolve_commerce_state(state, first)
    assert "name" not in first.commercial_data["missing_fields"]
    assert "email" in first.commercial_data["missing_fields"]

    second = update_checkout_data(state, {"email": "JOAO@example.com"})
    state = evolve_commerce_state(state, second)
    assert state.checkout_draft.customer.name == "Joao"
    assert state.checkout_draft.customer.cpf == "52998224725"
    assert state.checkout_draft.customer.email == "joao@example.com"

    address = update_checkout_data(state, {
        "phone": "(55) 11 99999-9999", "address": "Rua Um",
        "zipcode": "19900-000", "number": "10", "neighborhood": "Centro",
        "city": "Ourinhos", "state": "sp",
    })
    assert address.commercial_data["missing_fields"] == []


@pytest.mark.asyncio
async def test_prepare_requires_shipping_and_email_then_sets_pending_when_ready():
    async def execute(_tool, _arguments):
        return _cart()

    no_shipping = await prepare_order(
        state=_ready_state(selected_shipping=None), execute=execute,
    )
    assert no_shipping.commercial_data["order_ready"] is False
    assert "selected_shipping" in no_shipping.commercial_data["missing_fields"]

    missing_email_state = _ready_state()
    missing_email_state.checkout_draft.customer.email = None
    missing_email = await prepare_order(state=missing_email_state, execute=execute)
    assert "email" in missing_email.commercial_data["missing_fields"]

    ready = await prepare_order(state=_ready_state(), execute=execute)
    assert ready.commercial_data["order_ready"] is True
    assert ready.commercial_data["display_total"] == "4735.09"
    assert ready.response_metadata["order_state"]["order_confirmation_status"] == "pending"
    assert ready.response_metadata["pending_action"] == "awaiting_order_confirmation"


@pytest.mark.asyncio
async def test_create_is_blocked_without_confirmation_and_stale_confirmation():
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        return _cart()

    blocked = await create_order(state=_ready_state(), execute=execute)
    assert blocked.safety_reason == "order_confirmation_required"
    assert calls == []

    prepared = await prepare_order(state=_ready_state(), execute=execute)
    state = evolve_commerce_state(_ready_state(), prepared)
    confirmed = evolve_commerce_state(state, confirm_prepared_order(state))
    confirmed.selected_shipping.price = "40.00"
    stale = await create_order(state=confirmed, execute=execute)
    assert stale.safety_reason == "order_confirmation_stale"
    stale_state = evolve_commerce_state(confirmed, stale)
    assert stale_state.order_confirmation_status == "not_ready"
    assert stale_state.order_review_version is None
    assert stale_state.confirmed_order_review_version is None
    assert "selected_shipping_not_in_active_quote" in stale.commercial_data["missing_fields"]
    assert not any(tool == "create_order" for tool, _ in calls)


def test_changing_checkout_zipcode_invalidates_shipping_quote():
    state = _ready_state()

    result = update_checkout_data(state, {"zipcode": "01001000"})
    updated = evolve_commerce_state(state, result)

    assert result.commercial_data["shipping_quote_required"] is True
    assert updated.shipping_quote_zipcode is None
    assert updated.shipping_quotes == []
    assert updated.selected_shipping is None


def test_new_cart_session_starts_new_order_scope():
    previous = _ready_state(
        order_id="123",
        order_session_id="SESSION-1",
        order_status="AGUARDANDO PAGAMENTO",
    )
    cart_result = AgentResult(
        reply_text="Carrinho novo.",
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "cart_state": {
                "cart_session_id": "SESSION-2",
                "cart_url": "https://loja.example/checkout/SESSION-2",
                "cart_items": [{
                    "product_id": "804", "variant_id": None, "quantity": 1,
                }],
            },
        },
    )

    current = evolve_commerce_state(previous, cart_result)

    assert current.cart_session_id == "SESSION-2"
    assert current.order_id is None
    assert current.order_session_id is None
    assert current.selected_shipping is None
    assert current.selected_payment_option is None


@pytest.mark.asyncio
async def test_create_persists_order_and_prevents_duplicate_post():
    posts = 0

    async def execute(tool, arguments):
        nonlocal posts
        if tool == "get_cart_complete":
            return _cart()
        if tool == "create_order":
            posts += 1
            assert arguments["products"][0]["product_id"] == "803"
            assert arguments["payment"] == {"method_id": "10545", "name": "Pix - Vindi"}
            return {"success": True, "order_id": 123, "code": 201, "status": "AGUARDANDO VINDI"}
        raise AssertionError(tool)

    base = _ready_state()
    prepared = evolve_commerce_state(base, await prepare_order(state=base, execute=execute))
    confirmed = evolve_commerce_state(prepared, confirm_prepared_order(prepared))
    created = await create_order(state=confirmed, execute=execute)
    state = evolve_commerce_state(confirmed, created)

    assert state.order_id == "123"
    assert state.purchase_stage == "order_created"
    assert state.order_confirmation_status == "not_ready"
    duplicate = await create_order(state=state, execute=execute)
    assert duplicate.commercial_data["existing"] is True
    assert posts == 1


@pytest.mark.asyncio
async def test_ambiguous_post_reconciles_by_session_id():
    async def execute(tool, arguments):
        if tool == "get_cart_complete":
            return _cart()
        if tool == "create_order":
            return {"error": "commerce_upstream_error", "status_code": 503}
        if tool == "list_orders":
            assert arguments == {"session_id": "SESSION-1"}
            return {"orders": [{"order_id": "321", "status": "AGUARDANDO PAGAMENTO"}]}
        raise AssertionError(tool)

    base = _ready_state()
    prepared = evolve_commerce_state(base, await prepare_order(state=base, execute=execute))
    confirmed = evolve_commerce_state(prepared, confirm_prepared_order(prepared))
    created = await create_order(state=confirmed, execute=execute)
    assert created.commercial_data["order_id"] == "321"


@pytest.mark.asyncio
async def test_created_order_survives_payment_lookup_failure(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, _arguments):
        if tool == "get_cart_complete":
            return _cart()
        if tool == "create_order":
            return {
                "success": True,
                "order_id": "777",
                "status": "AGUARDANDO VINDI",
            }
        if tool == "get_order_payment":
            return {
                "error": "commerce_upstream_error",
                "status_code": 500,
            }
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    base = _ready_state()
    prepared = evolve_commerce_state(
        base, await prepare_order(state=base, execute=execute),
    )
    confirmed = evolve_commerce_state(
        prepared, confirm_prepared_order(prepared),
    )

    result = await sales_agent._create_order_with_payment_lookup(confirmed)
    current = evolve_commerce_state(confirmed, result)

    assert result.commercial_data["success"] is True
    assert result.commercial_data["order_id"] == "777"
    assert result.commercial_data["payment"]["status"] == "unknown"
    assert result.safety_reason == "order_payment_technical_failure"
    assert current.order_id == "777"
    assert current.order_payment_status == "unknown"
    assert current.purchase_stage == "order_created"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,status_group",
    [
        ("AGUARDANDO VINDI", "awaiting_payment"),
        ("A ENVIAR VINDI", "awaiting_shipment"),
        ("ENVIADO", "shipped"),
        ("FINALIZADO", "completed"),
    ],
)
async def test_status_and_tracking_preserve_adapter_facts(status, status_group):
    async def execute(tool, arguments):
        assert tool == "get_order_complete"
        assert arguments == {"order_id": "123"}
        return {
            "order_id": "123", "status": status, "status_group": status_group,
            "sending_code": "TRACK123", "tracking_url": "https://track.example/123",
            "estimated_delivery_date": "2026-08-10",
        }

    result = await get_order_facts(
        state=_ready_state(order_id="123"), execute=execute,
    )
    assert result.commercial_data["status"] == status
    assert result.commercial_data["status_group"] == status_group
    assert result.commercial_data["tracking"] == {
        "sending_code": "TRACK123",
        "tracking_url": "https://track.example/123",
        "estimated_delivery_date": "2026-08-10",
    }


@pytest.mark.asyncio
async def test_tracking_absence_is_not_filled():
    async def execute(_tool, _arguments):
        return {"order_id": "123", "status": "A ENVIAR"}

    result = await get_order_facts(
        state=_ready_state(order_id="123"), execute=execute,
    )
    assert result.commercial_data["tracking"] == {}


@pytest.mark.asyncio
async def test_site_channel_cannot_start_whatsapp_shipping():
    async def never(*_args):
        raise AssertionError("site flow must not call shipping quote")

    state = _whatsapp_state(checkout_channel_preference="site")
    result = await quote_shipping(state=state, zipcode="19900000", execute=never)
    assert result.safety_reason == "whatsapp_order_channel_required"


@pytest.mark.asyncio
async def test_sales_agent_orchestrates_whatsapp_flow_without_real_openai(monkeypatch):
    import app.sales_agent as sales_agent

    posts = 0

    async def execute(tool, arguments):
        nonlocal posts
        if tool == "get_cart_complete":
            return _cart()
        if tool == "quote_shipping":
            return {"success": True, "options": [{
                "shipping_id": "1", "quotation_id": "Q1", "name": "PAC Tray",
                "price": "35.10", "min_period": 3, "max_period": 8,
            }]}
        if tool == "get_payment_options":
            return {"payment_options": {
                "pix": {"id": "10545", "name": "Pix - Vindi"},
                "options": [{"id": "10545", "name": "Pix - Vindi"}],
            }}
        if tool == "create_order":
            posts += 1
            return {"success": True, "order_id": "900", "status": "AGUARDANDO VINDI"}
        if tool == "get_order_payment":
            assert arguments == {"order_id": "900"}
            return {
                "success": True,
                "order_id": "900",
                "payment": {
                    "method_id": "10545",
                    "method": "Pix - Vindi",
                    "type": "pix",
                    "has_payment": False,
                    "payment_date": None,
                    "payment_url": "https://pay.example/order/900?token=official",
                },
            }
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = _whatsapp_state()

    async def turn(interpretation):
        nonlocal state
        result = await sales_agent.handle_sales_message(
            IncomingMessage(text="mensagem interpretada"), {}, {}, interpretation,
            commerce_state=state,
        )
        state = evolve_commerce_state(state, result)
        return result

    await turn(_interpretation(shipping_action="quote", shipping_zipcode="19900-000"))
    await turn(_interpretation(shipping_action="select", shipping_selection_position=1))
    await turn(_interpretation(
        checkout_data={
            "name": "Joao", "cpf": "52998224725", "email": "joao@example.com",
            "phone": "5511999999999", "address": "Rua Um", "zipcode": "19900000",
            "number": "10", "neighborhood": "Centro", "city": "Ourinhos", "state": "SP",
        }
    ))
    await turn(_interpretation(
        payment_action="payment_options", payment_method_preference="pix",
        information_needed=["payment"],
    ))
    prepared = await turn(_interpretation(checkout_action="prepare_order"))
    assert prepared.commercial_data["order_ready"] is True
    created = await turn(_interpretation(confirmation="confirm"))

    assert created.commercial_data["order_id"] == "900"
    assert created.commercial_data["payment"]["status"] == "pending"
    assert created.commercial_data["payment"]["payment_url"] == (
        "https://pay.example/order/900?token=official"
    )
    assert state.order_id == "900"
    assert state.purchase_stage == "awaiting_payment"
    assert state.order_payment_status == "pending"
    assert state.pending_action == "awaiting_payment"
    assert posts == 1
