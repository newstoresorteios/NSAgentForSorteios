import pytest

from app.commerce_context import CommerceConversationState
from app.context_resume import (
    build_contextual_greeting,
    commerce_state_resumable_score,
    has_resumable_commerce,
    is_presented_catalog_question,
    is_soft_greeting,
    is_unpaid_order_resume_request,
    merge_commerce_states,
    should_redisplay_presented_catalog,
    should_resume_pending_order,
)
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def test_merge_recovers_order_wiped_by_later_greeting():
    latest = {
        "active_domain": "commerce",
        "pending_action": None,
        "order_id": None,
    }
    previous = {
        "order_id": "0CC131B51070AEF",
        "order_payment_url": "https://pay.example/1",
        "pending_action": "awaiting_payment",
        "purchase_stage": "awaiting_payment",
        "active_product": {"product_id": "1", "name": "Seiko"},
    }
    merged = merge_commerce_states(latest, previous)
    assert merged["order_id"] == "0CC131B51070AEF"
    assert merged["order_payment_url"] == "https://pay.example/1"
    assert merged["pending_action"] == "awaiting_payment"


def test_merge_keeps_latest_presented_list_over_richer_cart_donor():
    latest = {
        "last_presented_products": [
            {"position": 1, "product_id": "1429", "name": "Seiko A", "brand": "Seiko"},
            {"position": 2, "product_id": "1945", "name": "Seiko B", "brand": "Seiko"},
            {"position": 3, "product_id": "1949", "name": "Seiko C", "brand": "Seiko"},
        ],
        "product_resolution_state": "options_presented",
        "active_topic": "seiko",
        "active_product": None,
        "pending_action": None,
    }
    previous = {
        "cart_session_id": "cart-old",
        "pending_action": "choose_checkout_channel",
        "active_product": {"product_id": "641", "name": "Tissot", "brand": "Tissot"},
        "last_presented_products": [
            {"position": 1, "product_id": "641", "name": "Tissot", "brand": "Tissot"},
            {"position": 2, "product_id": "651", "name": "Tissot azul", "brand": "Tissot"},
            {"position": 3, "product_id": "663", "name": "Citizen", "brand": "Citizen"},
        ],
    }
    merged = merge_commerce_states(latest, previous)
    assert merged["cart_session_id"] == "cart-old"
    assert merged["last_presented_products"][0]["product_id"] == "1429"
    assert merged["active_product"] is None
    assert merged["product_resolution_state"] == "options_presented"


def test_soft_greeting_and_unpaid_resume_detection():
    assert is_soft_greeting("Opa, boa noite")
    assert is_unpaid_order_resume_request(
        "acabamos de conversar, eu nao fiz o pagamento ainda"
    )
    state = CommerceConversationState(
        order_id="0CC131B51070AEF",
        order_payment_url="https://pay.example/1",
        pending_action="awaiting_payment",
    )
    # Unpaid checkout: greeting and generic buy resume the payment link.
    assert should_resume_pending_order("Opa, boa noite", state) is True
    assert should_resume_pending_order("BOm dia", state) is True
    assert should_resume_pending_order("Quero comprar um relógio", state) is True
    assert should_resume_pending_order("como esta meu pedido?", state) is False
    assert should_resume_pending_order(
        "acabamos de conversar, eu nao fiz o pagamento ainda",
        state,
    ) is True
    assert should_resume_pending_order("sim", state) is True
    assert should_resume_pending_order("nenhuma", state) is False
    assert should_resume_pending_order("Qual relógio?", state) is True

    listed = CommerceConversationState(
        order_id="25422",
        order_payment_url="https://pay.example/1",
        pending_action="awaiting_payment",
        last_presented_products=[
            {"position": 1, "product_id": "b1", "name": "Baltic Aquascaphe"},
        ],
    )
    assert should_resume_pending_order("Qual relógio?", listed) is False
    assert should_redisplay_presented_catalog("Qual relógio?", listed) is True
    assert should_redisplay_presented_catalog("qual desses?", listed) is True
    assert should_redisplay_presented_catalog("me mostra", listed) is True
    assert should_redisplay_presented_catalog("Qual relógio Seiko", listed) is False
    assert is_presented_catalog_question("qual o preço") is False
    assert should_redisplay_presented_catalog("nenhuma", listed) is False


def test_fallback_does_not_treat_qual_relogio_as_model():
    from app.sales.discovery import _specific_product_lock
    from app.sales_agent import _fallback_interpretation, deterministic_sales_plan

    plan = deterministic_sales_plan("Qual relógio?")
    assert plan is not None
    assert plan["subject"].get("model") in (None, "")
    interp = _fallback_interpretation("Qual relógio?")
    assert interp.subject.model in (None, "")
    assert interp.enough_information_to_search is False
    assert interp.ready_for_retrieval is False
    assert _specific_product_lock(
        SalesInterpretation(
            domain="commerce",
            goal="find",
            subject={"model": "Qual relógio"},
            references_previous_context=False,
            needs_clarification=False,
            confidence=0.6,
        )
    ) is False
    seiko = deterministic_sales_plan("Qual relógio Seiko")
    assert seiko is not None
    assert (seiko["subject"].get("brand") or "").casefold() == "seiko"


def test_contextual_greeting_is_soft_and_non_intrusive():
    state = CommerceConversationState(
        order_id="0CC131B51070AEF",
        order_payment_url="https://pay.example/1",
        pending_action="awaiting_payment",
        active_product={"product_id": "1", "name": "Seiko SRPD79K1"},
    )
    result = build_contextual_greeting(state)
    assert "0CC131B51070AEF" not in result.reply_text
    assert "https://pay.example/1" not in result.reply_text
    assert "Seiko" not in result.reply_text
    assert result.response_metadata["response_source"] == "context_resume_soft"


@pytest.mark.asyncio
async def test_pipeline_greeting_resumes_unpaid_order_link(monkeypatch):
    import app.message_pipeline as pipeline
    import app.openai_agent as openai_agent

    state = CommerceConversationState(
        order_id="0CC131B51070AEF",
        order_payment_url="https://pay.example/pedido",
        pending_action="awaiting_payment",
        purchase_stage="awaiting_payment",
        active_product={"product_id": "77", "name": "Seiko"},
    ).model_dump(mode="json")

    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "audio_inbound_enabled": False,
                "audio_outbound_enabled": False,
                "agent_policy_mode": "off",
                "agent_factual_validation_mode": "off",
                "agent_quality_judge_mode": "off",
                "agent_trusted_fact_domains": "",
                "max_reply_chars": 900,
            },
        )(),
    )
    monkeypatch.setattr(
        pipeline,
        "load_commerce_conversation_state",
        lambda **_kwargs: state,
    )
    monkeypatch.setattr(pipeline, "persist_customer_commerce_session", lambda **_kwargs: None)
    monkeypatch.setattr(pipeline, "upsert_customer_identity_links", lambda *a, **k: None)
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **_kwargs: [])
    monkeypatch.setattr(
        openai_agent,
        "interpret_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("soft greeting before interpret")),
    )
    monkeypatch.setattr(
        openai_agent,
        "inspect_order_payment",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no payment dump on greeting")),
    )

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-1",
        sender_phone="5543999999999",
        sender_key="whatsapp:5543999999999",
        text="Opa, boa noite",
        raw={"inbound_id": 10},
    )
    result = await pipeline.process_incoming_message(incoming, {"found": False})
    assert "0CC131B51070AEF" in result.reply_text
    assert "https://pay.example/pedido" in result.reply_text
    assert result.response_metadata["commerce_state"]["order_id"] == "0CC131B51070AEF"
    assert result.response_metadata["working_memory"]["payment_pending"] is True


@pytest.mark.asyncio
async def test_quero_comprar_relogio_resumes_joao_awaiting_payment(monkeypatch):
    import app.openai_agent as openai_agent

    state = {
        "order_id": "25422",
        "order_payment_url": "https://pay.example/25422",
        "pending_action": "awaiting_payment",
        "purchase_stage": "awaiting_payment",
        "order_payment_status": "pending",
        "cart_session_id": "cart-joao",
        "last_presented_products": [
            {"position": 1, "product_id": "b1", "name": "Baltic Aquascaphe"},
            {"position": 2, "product_id": "b2", "name": "Baltic Classic"},
            {"position": 3, "product_id": "b3", "name": "Baltic MR01"},
        ],
    }

    async def boom(*_a, **_k):
        raise AssertionError("must not interpret, search catalog, or call Tray payment")

    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **_k: [])
    monkeypatch.setattr(openai_agent, "detect_blocked_request", lambda _t: None)
    monkeypatch.setattr(openai_agent, "should_request_human_handoff", lambda _m, **_k: None)
    monkeypatch.setattr(openai_agent, "interpret_message", boom)
    monkeypatch.setattr(openai_agent, "inspect_order_payment", boom)
    monkeypatch.setattr(openai_agent, "handle_sales_message", boom)

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(
            text="Quero comprar um relógio",
            conversation_id="conv-joao",
            sender_phone="5548999490859",
        ),
        {"_commerce_state": state},
    )
    assert "25422" in result.reply_text
    assert "https://pay.example/25422" in result.reply_text
    assert "Baltic" not in result.reply_text
    assert result.response_metadata.get("response_source") == "context_resume_payment_url"


@pytest.mark.asyncio
async def test_qual_relogio_redisplays_joao_shortlist_without_tray(monkeypatch):
    import app.openai_agent as openai_agent

    state = {
        "order_id": "25422",
        "order_payment_url": "https://pay.example/25422",
        "pending_action": "awaiting_payment",
        "purchase_stage": "awaiting_payment",
        "order_payment_status": "pending",
        "cart_session_id": "cart-joao",
        "last_presented_products": [
            {"position": 1, "product_id": "b1", "name": "Relógio Baltic Aquascaphe Automático Titânio Azul"},
            {"position": 2, "product_id": "b2", "name": "Relógio Baltic Aquascaphe Classic Automático Preto SB01"},
            {"position": 3, "product_id": "b3", "name": "Relógio Baltic MR01 Automático Preto"},
        ],
    }

    async def boom(*_a, **_k):
        raise AssertionError("must not interpret, search catalog, or call Tray")

    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **_k: [])
    monkeypatch.setattr(openai_agent, "detect_blocked_request", lambda _t: None)
    monkeypatch.setattr(openai_agent, "should_request_human_handoff", lambda _m, **_k: None)
    monkeypatch.setattr(openai_agent, "interpret_message", boom)
    monkeypatch.setattr(openai_agent, "inspect_order_payment", boom)
    monkeypatch.setattr(openai_agent, "handle_sales_message", boom)

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(
            text="Qual relógio?",
            conversation_id="conv-joao",
            sender_phone="5548999490859",
        ),
        {"_commerce_state": state},
    )
    assert "Baltic Aquascaphe" in result.reply_text
    assert "Baltic MR01" in result.reply_text
    assert "https://pay.example/25422" not in result.reply_text
    assert result.response_metadata.get("response_source") == "context_resume_presented_catalog"
    assert result.response_metadata.get("pending_action") == "awaiting_payment"
    assert result.response_metadata.get("used_tray") is False


def test_product_match_failed_keeps_awaiting_payment():
    from app.commerce_context import evolve_commerce_state

    state = CommerceConversationState(
        order_id="25422",
        order_payment_url="https://pay.example/25422",
        pending_action="awaiting_payment",
        purchase_stage="awaiting_payment",
        last_presented_products=[
            {"position": 1, "product_id": "b1", "name": "Baltic Aquascaphe"},
        ],
    )
    result = AgentResult(
        reply_text="Não consegui consultar as informações da loja neste momento.",
        intent="commerce",
        safety_reason="product_match_failed",
        response_metadata={"clear_pending_action": True, "domain": "commerce"},
    )
    updated = evolve_commerce_state(state, result)
    assert updated.pending_action == "awaiting_payment"
    assert updated.order_id == "25422"


def test_presented_catalog_resume_survives_checkout_evolve():
    from app.commerce_context import evolve_commerce_state
    from app.context_resume import build_presented_catalog_resume_result

    state = CommerceConversationState(
        order_id="25422",
        order_payment_url="https://pay.example/25422",
        pending_action="awaiting_payment",
        purchase_stage="awaiting_payment",
        last_presented_products=[
            {"position": 1, "product_id": "b1", "name": "Baltic Aquascaphe"},
            {"position": 2, "product_id": "b2", "name": "Baltic Classic"},
            {"position": 3, "product_id": "b3", "name": "Baltic MR01"},
        ],
    )
    result = build_presented_catalog_resume_result(state)
    assert result is not None
    updated = evolve_commerce_state(state, result)
    assert updated.pending_action == "awaiting_payment"
    assert [item.product_id for item in updated.last_presented_products] == [
        "b1",
        "b2",
        "b3",
    ]


@pytest.mark.asyncio
async def test_order_status_uses_recovered_state_order_id(monkeypatch):
    import app.message_pipeline as pipeline
    import app.openai_agent as openai_agent

    state = {
        "order_id": "0CC131B51070AEF",
        "pending_action": "awaiting_payment",
        "order_payment_url": "https://pay.example/pedido",
    }
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "audio_inbound_enabled": False,
                "audio_outbound_enabled": False,
                "agent_policy_mode": "off",
                "agent_factual_validation_mode": "off",
                "agent_quality_judge_mode": "off",
                "agent_trusted_fact_domains": "",
                "max_reply_chars": 900,
            },
        )(),
    )
    monkeypatch.setattr(
        pipeline,
        "load_commerce_conversation_state",
        lambda **_kwargs: state,
    )
    monkeypatch.setattr(pipeline, "persist_customer_commerce_session", lambda **_kwargs: None)
    monkeypatch.setattr(pipeline, "upsert_customer_identity_links", lambda *a, **k: None)
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **_kwargs: [])

    async def fake_facts(*, state, execute, order_id=None, allow_customer_recovery=True):
        assert order_id is None
        assert state.order_id == "0CC131B51070AEF"
        return AgentResult(
            reply_text="Pedido 0CC131B51070AEF aguardando pagamento.",
            intent="commerce",
            response_metadata={"domain": "commerce", "used_tray": True},
        )

    monkeypatch.setattr(openai_agent, "get_order_facts", fake_facts)
    monkeypatch.setattr(
        openai_agent,
        "interpret_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no interpret")),
    )

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-1",
        sender_phone="5543999999999",
        text="como esta meu pedido?",
        raw={"inbound_id": 11},
    )
    result = await pipeline.process_incoming_message(incoming, {"found": False})
    assert "0CC131B51070AEF" in result.reply_text


def test_resumable_score_prefers_order_over_empty_greeting_state():
    assert commerce_state_resumable_score({"order_id": "1"}) > commerce_state_resumable_score({})
    assert has_resumable_commerce({"cart_session_id": "abc"}) is True


def test_identity_person_key_prefers_cpf():
    from app.customer_identity import person_key_from_parts

    assert person_key_from_parts(cpf="12345678901", phone="5543999999999") == "cpf:12345678901"
    assert person_key_from_parts(phone="5543999999999") == "phone:5543999999999"
    assert person_key_from_parts(sender_key="instagram:abc") == "sender:instagram:abc"


def test_working_memory_is_compact_and_policy_aware():
    from app.working_memory import WORKING_MEMORY_USAGE_POLICY, build_working_memory

    memory = build_working_memory(
        CommerceConversationState(
            order_id="0CC131B51070AEF",
            order_payment_url="https://pay.example/1",
            pending_action="awaiting_payment",
            active_product={"product_id": "1", "name": "Seiko"},
            checkout_draft={
                "customer": {
                    "name": "Paulo",
                    "cpf": "07281035918",
                    "email": "a@b.com",
                    "phone": "85999498149",
                }
            },
        )
    )
    assert memory["has_open_order"] is True
    assert memory["payment_pending"] is True
    assert memory["known_checkout_fields"]["cpf"] is True
    assert "07281035918" not in str(memory["known_checkout_fields"])
    assert memory["usage_policy"] == WORKING_MEMORY_USAGE_POLICY


@pytest.mark.asyncio
async def test_pipeline_always_passes_sender_key_with_conversation_id(monkeypatch):
    import app.message_pipeline as pipeline

    seen = {}

    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "audio_inbound_enabled": False,
                "audio_outbound_enabled": False,
                "agent_policy_mode": "off",
                "agent_factual_validation_mode": "off",
                "agent_quality_judge_mode": "off",
                "agent_trusted_fact_domains": "",
                "max_reply_chars": 900,
            },
        )(),
    )

    def load_state(**kwargs):
        seen.update(kwargs)
        return {}

    async def generate(incoming, customer_context):
        return AgentResult(reply_text="ok", intent="general")

    monkeypatch.setattr(pipeline, "load_commerce_conversation_state", load_state)
    monkeypatch.setattr(pipeline, "generate_agent_reply_async", generate)
    monkeypatch.setattr(pipeline, "upsert_customer_identity_links", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "persist_customer_commerce_session", lambda **_kwargs: None)

    await pipeline.process_incoming_message(
        IncomingMessage(
            channel="whatsapp",
            conversation_id="conv-new",
            sender_key="whatsapp:5543999999999",
            sender_phone="5543999999999",
            text="oi",
            raw={"inbound_id": 99},
        ),
        {"found": False},
    )
    assert seen["sender_key"] == "whatsapp:5543999999999"
    assert seen["conversation_id"] == "conv-new"
