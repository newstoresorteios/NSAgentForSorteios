from types import SimpleNamespace

import pytest

from app.commerce_context import (
    CommerceConversationState,
    apply_commerce_domain_context,
    evolve_commerce_state,
)
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _interpretation(**overrides) -> SalesInterpretation:
    payload = {
        "domain": "commerce",
        "goal": "buy",
        "subject": {"product_type": "produto"},
        "preferences": {},
        "information_needed": ["catalog"],
        "references_previous_context": True,
        "needs_clarification": False,
        "purchase_action": "create_cart",
        "purchase_stage": "selection",
        "confidence": 0.99,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


def _state(**overrides) -> CommerceConversationState:
    payload = {
        "active_domain": "commerce",
        "last_presented_products": [
            {"position": 1, "product_id": "A", "name": "Produto A"},
            {"position": 2, "product_id": "B", "name": "Produto B"},
            {"position": 3, "product_id": "C", "name": "Produto C"},
        ],
        "purchase_stage": "selection",
    }
    payload.update(overrides)
    return CommerceConversationState(**payload)


def _settings():
    return SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini")


def test_structured_plan_preserves_cart_action_and_quantity():
    from app.sales_agent import interpretation_to_plan

    plan = interpretation_to_plan(
        _interpretation(
            purchase_action="create_cart",
            quantity=3,
            reference_type="current_product",
        )
    )

    assert plan["purchase_action"] == "create_cart"
    assert plan["quantity"] == 3


def test_structured_plan_preserves_multiple_purchase_items():
    from app.sales_agent import interpretation_to_plan

    plan = interpretation_to_plan(
        _interpretation(
            purchase_items=[
                {
                    "reference_type": "list_position",
                    "reference_position": 1,
                    "quantity": 2,
                },
                {
                    "reference_type": "list_position",
                    "reference_position": 2,
                    "quantity": 1,
                },
            ],
        )
    )

    assert plan["purchase_items"] == [
        {
            "reference_type": "list_position",
            "reference_position": 1,
            "explicit_product_name": None,
            "quantity": 2,
        },
        {
            "reference_type": "list_position",
            "reference_position": 2,
            "explicit_product_name": None,
            "quantity": 1,
        },
    ]


@pytest.mark.asyncio
async def test_list_selection_revalidates_price_quantity_and_creates_cart(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "B",
                "name": "Produto B atualizado",
                "current_price": "125.50",
                "available": True,
                "has_variation": False,
            }
        if tool == "create_cart":
            return {
                "cart_id": "CART-1",
                "session_id": "SESSION-1",
                "cart_url": "https://loja.example/checkout/SESSION-1",
            }
        if tool == "get_cart_complete":
            return {
                "cart_id": "CART-1",
                "session_id": "SESSION-1",
                "total": "251.00",
                "items": [{"product_id": "B", "quantity": 2}],
            }
        raise AssertionError(f"unexpected tool {tool}")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    interpretation = _interpretation(
        reference_type="list_position",
        reference_position=2,
        quantity=2,
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção de compra"),
        {"primary_intent": "commerce"},
        {},
        interpretation,
        commerce_state=_state(),
    )

    assert result is not None
    assert calls[0] == ("get_product", {"product_id": "B"})
    assert calls[1][0] == "create_cart"
    create_payload = calls[1][1]
    assert create_payload["product_id"] == "B"
    assert create_payload["variant_id"] is None
    assert create_payload["quantity"] == 2
    assert create_payload["price"] == "125.50"
    assert len(create_payload["session_id"]) == 32
    int(create_payload["session_id"], 16)
    assert calls[2] == ("get_cart_complete", {"session_id": "SESSION-1"})
    assert result.response_metadata["purchase_stage"] == "cart_created"
    assert result.response_metadata["cart_state"]["cart_product_id"] == "B"
    assert result.response_metadata["cart_state"]["cart_quantity"] == 2
    assert result.commercial_data["current_price"] == "125.50"


@pytest.mark.asyncio
async def test_unavailable_product_does_not_create_cart(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        assert tool == "get_product"
        return {
            "id": "B",
            "price": "90.00",
            "available": False,
            "available_in_store": False,
        }

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção"),
        {},
        {},
        _interpretation(reference_type="list_position", reference_position=2),
        commerce_state=_state(),
    )

    assert result is not None
    assert result.safety_reason == "product_unavailable"
    assert [tool for tool, _ in calls] == ["get_product"]


@pytest.mark.asyncio
async def test_single_variant_is_validated_and_used(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "B",
                "price": "100.00",
                "available": True,
                "has_variation": True,
            }
        if tool == "list_product_variants":
            return {
                "variants": [{
                    "id": "V1",
                    "product_id": "B",
                    "price": "95.00",
                    "available": True,
                }]
            }
        if tool == "create_cart":
            return {
                "cart_id": "C1",
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }
        if tool == "get_cart_complete":
            return {
                "items": [{"product_id": "B", "variant_id": "V1", "quantity": 1}],
                "total": "95.00",
            }
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção"),
        {},
        {},
        _interpretation(reference_type="list_position", reference_position=2),
        commerce_state=_state(),
    )

    assert result is not None
    create_call = next(arguments for tool, arguments in calls if tool == "create_cart")
    assert create_call["variant_id"] == "V1"
    assert create_call["price"] == "95.00"
    assert result.response_metadata["active_product"]["variant_id"] == "V1"


@pytest.mark.asyncio
async def test_multiple_variants_require_choice_before_cart(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "B",
                "price": "100.00",
                "available": True,
                "has_variation": True,
            }
        if tool == "list_product_variants":
            return {
                "variants": [
                    {"id": "V1", "available": True, "color": "Preto"},
                    {"id": "V2", "available": True, "color": "Azul"},
                ]
            }
        raise AssertionError("cart must not be created")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção"),
        {},
        {},
        _interpretation(reference_type="list_position", reference_position=2),
        commerce_state=_state(),
    )

    assert result is not None
    assert result.safety_reason == "variant_required"
    assert [tool for tool, _ in calls] == ["get_product", "list_product_variants"]


@pytest.mark.asyncio
async def test_existing_cart_link_is_reused_without_new_post(monkeypatch):
    import app.sales_agent as sales_agent

    async def never_execute(*_args, **_kwargs):
        raise AssertionError("stored checkout link must not create another cart")

    monkeypatch.setattr(sales_agent, "execute_tool", never_execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = _state(
        active_product={"product_id": "B", "name": "Produto B"},
        cart_id="C1",
        cart_session_id="S1",
        cart_url="https://loja.example/checkout/S1",
        cart_product_id="B",
        cart_quantity=1,
        purchase_stage="cart_created",
    )
    interpretation = _interpretation(
        goal="inspect",
        purchase_action="show_cart_link",
        reference_type=None,
        purchase_stage="cart_created",
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="link atual"),
        {},
        {},
        interpretation,
        commerce_state=state,
    )

    assert result is not None
    assert "https://loja.example/checkout/S1" in result.reply_text
    assert result.response_metadata["used_tray"] is False


@pytest.mark.asyncio
async def test_repeated_cart_creation_reuses_same_checkout_without_post(monkeypatch):
    import app.sales_agent as sales_agent

    async def never_execute(*_args, **_kwargs):
        raise AssertionError("same cart selection must be idempotent")

    monkeypatch.setattr(sales_agent, "execute_tool", never_execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = _state(
        active_product={"product_id": "B", "name": "Produto B"},
        cart_id="C1",
        cart_session_id="S1",
        cart_url="https://loja.example/checkout/S1",
        cart_product_id="B",
        cart_quantity=1,
        purchase_stage="cart_created",
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção repetida"),
        {},
        {},
        _interpretation(reference_type="current_product"),
        commerce_state=state,
    )

    assert result is not None
    assert "https://loja.example/checkout/S1" in result.reply_text
    assert result.response_metadata["used_tray"] is False


@pytest.mark.asyncio
async def test_persistent_cart_state_is_loaded_by_evolution():
    from app.cart_service import create_cart_checkout

    async def execute(tool, arguments):
        if tool == "get_product":
            return {"id": "B", "price": "10.00", "available": True}
        if tool == "create_cart":
            return {
                "cart_id": "C1",
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }
        if tool == "get_cart_complete":
            return {
                "items": [{"product_id": "B", "quantity": 1}],
                "total": "10.00",
            }
        raise AssertionError(tool)

    previous = _state(active_product={"product_id": "B", "name": "Produto B"})
    result = await create_cart_checkout(
        interpretation=_interpretation(reference_type="current_product"),
        product_reference=previous.active_product,
        state=previous,
        execute=execute,
    )
    updated = evolve_commerce_state(previous, result)

    assert updated.cart_id == "C1"
    assert updated.cart_session_id == "S1"
    assert updated.cart_url == "https://loja.example/checkout/S1"
    assert updated.purchase_stage == "cart_created"
    assert updated.active_product.product_id == "B"


@pytest.mark.asyncio
async def test_cart_adapter_failure_is_technical_not_product_not_found(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        if tool == "get_product":
            return {"id": "B", "price": "10.00", "available": True}
        if tool == "create_cart":
            return {"error": "unavailable", "status_code": 503}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção"),
        {},
        {},
        _interpretation(reference_type="list_position", reference_position=2),
        commerce_state=_state(),
    )

    assert result is not None
    assert result.safety_reason == "cart_technical_failure"
    assert result.response_metadata["response_source"] == "technical_fallback"


@pytest.mark.asyncio
async def test_failed_cart_for_new_selection_keeps_new_active_product(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        assert tool == "get_product"
        return {
            "id": "B",
            "price": "10.00",
            "available": False,
            "available_in_store": False,
        }

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    previous = _state(
        active_product={"product_id": "A", "name": "Produto A"},
        cart_session_id="OLD",
        cart_url="https://loja.example/checkout/OLD",
        cart_product_id="A",
        cart_quantity=1,
    )
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="nova seleção"),
        {},
        {},
        _interpretation(reference_type="list_position", reference_position=2),
        commerce_state=previous,
    )
    updated = evolve_commerce_state(previous, result)

    assert updated.active_product.product_id == "B"
    assert updated.cart_product_id == "A"
    assert updated.purchase_stage == "selection"


def test_checkout_question_uses_structured_commerce_domain():
    state = _state(
        active_product={"product_id": "B"},
        cart_session_id="S1",
        cart_url="https://loja.example/checkout/S1",
        purchase_stage="cart_created",
    )
    interpretation = _interpretation(
        domain="commerce",
        purchase_action="checkout_question",
        purchase_stage="cart_created",
        domain_change_explicit=False,
    )

    contextual, changed = apply_commerce_domain_context(interpretation, state)

    assert changed is False
    assert contextual.domain == "commerce"
    assert contextual.purchase_action == "checkout_question"


@pytest.mark.asyncio
async def test_active_product_purchase_does_not_search_by_name(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {"id": "B", "price": "10.00", "available": True}
        if tool == "create_cart":
            return {
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }
        if tool == "get_cart_complete":
            return {
                "items": [{"product_id": "B", "quantity": 1}],
                "total": "10.00",
            }
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = _state(active_product={"product_id": "B", "name": "Produto B"})
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="produto atual"),
        {},
        {},
        _interpretation(reference_type="current_product"),
        commerce_state=state,
    )

    assert result is not None
    assert [tool for tool, _ in calls] == [
        "get_product",
        "create_cart",
        "get_cart_complete",
    ]
    assert all(tool != "search_products" for tool, _ in calls)


@pytest.mark.asyncio
async def test_cart_success_uses_openai_sales_responder(monkeypatch):
    import app.sales_agent as sales_agent

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                "Pronto, seu produto está no carrinho. "
                                "Finalize pelo checkout oficial informado."
                            )
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="test-key",
            openai_model="gpt-4.1-mini",
        ),
    )
    monkeypatch.setattr(sales_agent, "AsyncOpenAI", FakeOpenAI)
    tray_result = AgentResult(
        reply_text="fallback",
        intent="commerce",
        commercial_data={
            "cart": {
                "status": "cart_created",
                "cart_url": "https://loja.example/checkout/S1",
            }
        },
        response_metadata={"purchase_stage": "cart_created", "used_tray": True},
    )
    interpretation = _interpretation()

    result = await sales_agent._sales_response_with_openai(
        IncomingMessage(text="compra confirmada"),
        {"goal": "buy"},
        tray_result,
        interpretation,
    )

    assert result is not None
    assert result.response_metadata["response_source"] == "openai"
    assert result.response_metadata["purchase_stage"] == "cart_created"
    assert "cartão, CVV" in captured["messages"][0]["content"]
