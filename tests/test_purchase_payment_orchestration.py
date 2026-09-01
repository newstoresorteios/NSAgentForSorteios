from types import SimpleNamespace

import pytest

from app.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _interpretation(**overrides) -> SalesInterpretation:
    payload = {
        "domain": "commerce",
        "goal": "buy",
        "subject": {"product_type": "produto"},
        "preferences": {},
        "information_needed": ["payment"],
        "references_previous_context": True,
        "needs_clarification": False,
        "payment_action": "payment_options",
        "confidence": 0.99,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


def _state(**overrides) -> CommerceConversationState:
    payload = {
        "active_domain": "commerce",
        "active_product": {
            "product_id": "A",
            "name": "Produto A",
        },
        "purchase_stage": "selection",
    }
    payload.update(overrides)
    return CommerceConversationState(**payload)


def _settings():
    return SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini")


def _cart_executor(*, pix=True, create_error=False, payment_error=False):
    calls = []
    posted_items = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": arguments["product_id"],
                "name": f"Produto {arguments['product_id']}",
                "current_price": "125.50",
                "available": True,
                "has_variation": False,
            }
        if tool == "create_cart":
            if create_error:
                return {"error": "adapter unavailable", "status_code": 503}
            posted_items.append(dict(arguments))
            return {
                "cart_id": "C1",
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }
        if tool == "get_cart_complete":
            return {
                "items": [
                    {
                        "product_id": item["product_id"],
                        "variant_id": item.get("variant_id"),
                        "quantity": item["quantity"],
                    }
                    for item in posted_items
                ],
                "total": "251.00",
            }
        if tool == "get_payment_options":
            if payment_error:
                return {"error": "adapter unavailable", "status_code": 503}
            options = {
                "installments": [
                    {"count": 10, "value": 25.10, "interest": False},
                ],
            }
            if pix:
                options["pix"] = {"value": 240.00}
            return {"payment_options": options}
        raise AssertionError(f"unexpected tool: {tool}")

    return execute, calls


def test_structured_plan_preserves_payment_preference_with_purchase_action():
    from app.sales_agent import interpretation_to_plan

    plan = interpretation_to_plan(
        _interpretation(
            purchase_action="create_cart",
            payment_method_preference="pix",
        )
    )

    assert plan["purchase_action"] == "create_cart"
    assert plan["payment_action"] == "payment_options"
    assert plan["payment_method_preference"] == "pix"


@pytest.mark.asyncio
async def test_explicit_payment_commitment_with_active_product_creates_cart_before_options(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor()
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="preferência de pagamento"),
        {},
        {},
        _interpretation(
            purchase_action="create_cart",
            reference_type="current_product",
            payment_method_preference="pix",
        ),
        commerce_state=_state(),
    )

    assert [name for name, _ in calls] == [
        "get_product",
        "create_cart",
        "get_cart_complete",
        "get_cart_complete",
        "get_payment_options",
    ]
    assert result.response_metadata["cart_state"]["cart_session_id"] == "S1"
    assert result.commercial_data["requested_method_available"] is True


@pytest.mark.asyncio
async def test_checkout_question_with_selected_product_ensures_cart(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor()
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="finalização da compra"),
        {},
        {},
        _interpretation(
            payment_action=None,
            payment_method_preference=None,
            purchase_action="checkout_question",
            reference_type="current_product",
        ),
        commerce_state=_state(),
    )

    assert [name for name, _ in calls] == [
        "get_product",
        "create_cart",
        "get_cart_complete",
    ]
    assert result.response_metadata["cart_state"]["cart_session_id"] == "S1"


@pytest.mark.asyncio
async def test_explicit_purchase_item_and_payment_execute_both_actions(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor()
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = _state(
        active_product=None,
        last_presented_products=[
            {"position": 1, "product_id": "T1", "name": "Tissot Chronograph"},
        ],
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="compra composta"),
        {},
        {},
        _interpretation(
            purchase_action="create_cart",
            payment_method_preference="pix",
            purchase_items=[
                {
                    "reference_type": "explicit_product",
                    "explicit_product_name": "Tissot Chronograph",
                    "quantity": 1,
                }
            ],
        ),
        commerce_state=state,
    )

    assert any(name == "create_cart" for name, _ in calls)
    assert calls[-1][0] == "get_payment_options"
    assert result.response_metadata["cart_state"]["cart_product_id"] == "T1"


@pytest.mark.asyncio
async def test_explicit_subject_is_resolved_before_cart_and_payment(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor()
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    async def retrieve(_interpretation, **_kwargs):
        return AgentResult(
            reply_text="produto resolvido",
            intent="commerce",
            commercial_data={
                "products": [
                    {"id": "T2", "name": "Produto explícito", "available": True},
                ]
            },
            response_metadata={"used_tray": True},
        )

    monkeypatch.setattr(sales_agent, "_execute_compiled_product_retrieval", retrieve)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="compra explícita com pagamento"),
        {},
        {},
        _interpretation(
            subject={"product_type": "produto", "brand": "Marca", "model": "Modelo"},
            purchase_action="create_cart",
            payment_method_preference="pix",
        ),
        commerce_state=CommerceConversationState(active_domain="commerce"),
    )

    post = next(arguments for name, arguments in calls if name == "create_cart")
    assert post["product_id"] == "T2"
    assert calls[-1][0] == "get_payment_options"
    assert result.response_metadata["cart_state"]["cart_product_id"] == "T2"


@pytest.mark.asyncio
async def test_existing_cart_queries_payment_without_new_cart(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor()
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    await sales_agent.handle_sales_message(
        IncomingMessage(text="forma escolhida"),
        {},
        {},
        _interpretation(payment_method_preference="pix"),
        commerce_state=_state(
            cart_session_id="S1",
            cart_url="https://loja.example/checkout/S1",
        ),
    )

    assert calls == [
        ("get_cart_complete", {"session_id": "S1"}),
        ("get_payment_options", {"cart_session_id": "S1"}),
    ]


@pytest.mark.asyncio
async def test_payment_without_product_does_not_create_arbitrary_cart(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor()
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="consulta de pagamento"),
        {},
        {},
        _interpretation(
            payment_method_preference=None,
            payment_request_kind="informational",
            goal="inspect",
        ),
        commerce_state=CommerceConversationState(active_domain="commerce"),
    )

    assert calls == []
    assert result.safety_reason == "informational_payment_no_cart"
    assert result.commercial_data["payment_policy"]["pix_discount_percent"] == 15
    assert "carrinho" not in result.reply_text.lower()


@pytest.mark.asyncio
async def test_payment_with_unselected_product_list_answers_pix_policy(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor()
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="faz 20% no pix de desconto"),
        {},
        {},
        _interpretation(
            payment_method_preference="pix",
            payment_request_kind="informational",
            goal="inspect",
        ),
        commerce_state=CommerceConversationState(
            active_domain="commerce",
            last_presented_products=[
                {"position": 1, "product_id": "A", "name": "Produto A"},
                {"position": 2, "product_id": "B", "name": "Produto B"},
                {"position": 3, "product_id": "C", "name": "Produto C"},
            ],
        ),
    )

    assert calls == []
    assert result.safety_reason == "informational_payment_no_cart"
    assert "15%" in result.reply_text
    assert "20%" not in result.reply_text or "não consigo" in result.reply_text.lower()
    assert result.commercial_data["cart"]["status"] == "not_required_for_informational_payment"


@pytest.mark.asyncio
async def test_checkout_payment_without_product_still_requires_selection(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor()
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="quero pagar no pix agora"),
        {},
        {},
        _interpretation(
            payment_method_preference="pix",
            payment_request_kind="checkout",
            goal="buy",
        ),
        commerce_state=CommerceConversationState(
            active_domain="commerce",
            last_presented_products=[
                {"position": 1, "product_id": "A", "name": "Produto A"},
                {"position": 2, "product_id": "B", "name": "Produto B"},
            ],
        ),
    )

    assert calls == []
    assert result.safety_reason == "product_ambiguous"
    assert "formas de pagamento" in result.reply_text.lower()
    assert "preparar o carrinho" not in result.reply_text.lower()


def test_informational_payment_uses_persona_runtime_discount(monkeypatch):
    import app.sales.policies.action_authority as authority
    from app.commerce_context import CommerceConversationState
    from app.persona_models import PersonaVersion
    from app.persona_runtime import (
        build_persona_runtime,
        reset_persona_runtime,
        set_persona_runtime,
    )

    persona = PersonaVersion.model_validate(
        {
            "id": 17,
            "tenant_id": "newstore",
            "persona_key": "newstore_commercial",
            "version": 17,
            "name": "Crono",
            "instructions": "PIX 10%",
            "instructions_hash": "x",
            "status": "active",
            "metadata": {
                "runtime_policy": {"pix_discount_percent": 10},
            },
        }
    )
    runtime = build_persona_runtime(active=persona)
    token = set_persona_runtime(runtime)
    try:
        result = authority.informational_payment_policy_result(
            CommerceConversationState(),
            payment_method_preference="pix",
        )
        assert "10%" in result.reply_text
        assert result.commercial_data["payment_policy"]["pix_discount_percent"] == 10
    finally:
        reset_persona_runtime(token)


@pytest.mark.asyncio
async def test_informational_payment_respects_require_cart_flag(monkeypatch):
    import app.sales_agent as sales_agent
    from app.persona_models import PersonaVersion
    from app.persona_runtime import (
        build_persona_runtime,
        reset_persona_runtime,
        set_persona_runtime,
    )

    execute, calls = _cart_executor()
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    persona = PersonaVersion.model_validate(
        {
            "id": 17,
            "tenant_id": "newstore",
            "persona_key": "newstore_commercial",
            "version": 17,
            "name": "Crono",
            "instructions": "x",
            "instructions_hash": "x",
            "status": "active",
            "metadata": {
                "runtime_policy": {
                    "require_cart_for_informational_payment": True,
                },
            },
        }
    )
    token = set_persona_runtime(build_persona_runtime(active=persona))
    try:
        result = await sales_agent.handle_sales_message(
            IncomingMessage(text="faz 20% no pix"),
            {},
            {},
            _interpretation(
                payment_method_preference="pix",
                payment_request_kind="informational",
                goal="inspect",
            ),
            commerce_state=CommerceConversationState(
                active_domain="commerce",
                last_presented_products=[
                    {"position": 1, "product_id": "A", "name": "Produto A"},
                ],
            ),
        )
    finally:
        reset_persona_runtime(token)

    assert calls == []
    assert result.safety_reason == "product_ambiguous"
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor()
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = CommerceConversationState(
        active_domain="commerce",
        last_presented_products=[
            {"position": 1, "product_id": "A", "name": "Produto A"},
            {"position": 2, "product_id": "B", "name": "Produto B"},
        ],
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="compra de itens selecionados"),
        {},
        {},
        _interpretation(
            purchase_action="create_cart",
            payment_method_preference="pix",
            purchase_items=[
                {"reference_type": "list_position", "reference_position": 1, "quantity": 1},
                {"reference_type": "list_position", "reference_position": 2, "quantity": 1},
            ],
        ),
        commerce_state=state,
    )

    posts = [arguments for name, arguments in calls if name == "create_cart"]
    assert len(posts) == 2
    assert len(posts[0]["session_id"]) == 32
    int(posts[0]["session_id"], 16)
    assert posts[1]["session_id"] == "S1"
    assert calls[-1][0] == "get_payment_options"
    assert len(result.response_metadata["cart_state"]["cart_items"]) == 2


@pytest.mark.asyncio
async def test_quantity_is_preserved_when_cart_is_ensured_for_payment(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor()
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    await sales_agent.handle_sales_message(
        IncomingMessage(text="quantidade e pagamento"),
        {},
        {},
        _interpretation(
            purchase_action="create_cart",
            payment_method_preference="pix",
            reference_type="current_product",
            quantity=2,
        ),
        commerce_state=_state(),
    )

    post = next(arguments for name, arguments in calls if name == "create_cart")
    assert post["quantity"] == 2


@pytest.mark.asyncio
async def test_unavailable_payment_method_is_not_claimed(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor(pix=False)
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="forma escolhida"),
        {},
        {},
        _interpretation(payment_method_preference="pix"),
        commerce_state=_state(
            cart_session_id="S1",
            cart_url="https://loja.example/checkout/S1",
        ),
    )

    assert calls[-1][0] == "get_payment_options"
    assert result.safety_reason == "payment_method_unavailable"
    assert result.commercial_data["requested_method_available"] is False


@pytest.mark.asyncio
async def test_cart_failure_stops_before_payment_options(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor(create_error=True)
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="compra com pagamento"),
        {},
        {},
        _interpretation(
            purchase_action="create_cart",
            reference_type="current_product",
            payment_method_preference="pix",
        ),
        commerce_state=_state(),
    )

    assert result.safety_reason == "cart_technical_failure"
    assert not any(name == "get_payment_options" for name, _ in calls)


@pytest.mark.asyncio
async def test_payment_failure_after_cart_keeps_created_cart_state(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor(payment_error=True)
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="compra com consulta de pagamento"),
        {},
        {},
        _interpretation(
            purchase_action="create_cart",
            reference_type="current_product",
            payment_method_preference="pix",
        ),
        commerce_state=_state(),
    )

    assert calls[-1][0] == "get_payment_options"
    assert result.safety_reason == "payment_options_technical_failure"
    assert result.response_metadata["cart_state"]["cart_session_id"] == "S1"
    assert result.commercial_data["checkout"]["cart_ready"] is True
    assert "cart_url" not in result.commercial_data["checkout"]
    assert result.response_metadata["cart_state"]["cart_url"].endswith(
        "/checkout/S1"
    )


@pytest.mark.asyncio
async def test_existing_cart_installment_uses_real_plot(monkeypatch):
    import app.sales_agent as sales_agent

    execute, _calls = _cart_executor()
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="parcelamento"),
        {},
        {},
        _interpretation(
            goal="inspect",
            payment_action="installment",
            installment_count=10,
        ),
        commerce_state=_state(
            cart_session_id="S1",
            cart_url="https://loja.example/checkout/S1",
        ),
    )

    assert result.commercial_data["requested_installment"] == {
        "count": 10,
        "value": 25.10,
        "interest": False,
    }
