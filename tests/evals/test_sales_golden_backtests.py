"""Golden multi-turn sales backtests (offline, no live OpenAI/Tray).

Locks the Crono discovery → shortlist → selection contracts that ChatBo
persona rules must drive.
"""

from __future__ import annotations

import pytest

from app.commerce_context import CommerceConversationState, resolve_commerce_reference
from app.context_resume import merge_commerce_states
from app.models import SalesInterpretation
from app.persona_models import PersonaVersion
from app.persona_runtime import (
    build_persona_runtime,
    parse_recommendation_policy,
    reset_persona_runtime,
    set_persona_runtime,
)
from app.product_retrieval import (
    apply_persona_presentation_order,
    customer_result_limit,
)


def _crono_chatbo_profile() -> dict:
    return {
        "name": "Crono New Store",
        "tone": "consultative",
        "greeting": "Olá! Eu sou o Crono, assistente virtual da New Store Relógios.",
        "qualification_rules": [
            "Você já tem um modelo em mente ou quer uma sugestão?",
            "Qual faixa de investimento você tem em mente?",
            "É para uso no dia a dia, trabalho, esporte ou uma ocasião especial?",
        ],
        "recommendation_rules": [
            "Recomendar somente peças que existem no catálogo integrado, sempre com o link oficial",
            "Se o cliente tem urgência, priorizar pronta entrega (2 a 5 dias úteis)",
            "Justificar toda recomendação com uma razão concreta ligada ao que o cliente disse",
            "Nunca apresentar mais de 3 peças de uma vez",
        ],
        "sales_goals": [
            "OBJETIVO — Fechar a venda dentro da conversa, enviando o link oficial do produto",
        ],
        "objection_handling": [
            "Se o cliente pedir desconto além do PIX, explicar a política e oferecer consultor humano",
        ],
        "examples": [
            "Cliente: quero um Seiko. Crono: pergunta faixa e estilo antes de listar.",
        ],
    }


def _persona() -> PersonaVersion:
    return PersonaVersion.model_validate(
        {
            "id": 20,
            "tenant_id": "newstore",
            "persona_key": "newstore_commercial",
            "version": 20,
            "name": "Crono",
            "instructions": "Eu sou o Crono. 15% no PIX.",
            "instructions_hash": "golden",
            "status": "active",
            "metadata": {"chatboPersonaId": "11111111-1111-1111-1111-111111111111"},
        }
    )


@pytest.mark.offline_eval
def test_parse_recommendation_policy_from_chatbo_rules():
    policy = parse_recommendation_policy(
        [
            "Nunca apresentar mais de 3 peças de uma vez",
            "Se o cliente tem urgência, priorizar pronta entrega",
            "sempre com o link oficial",
            "Justificar toda recomendação",
        ]
    )
    assert policy["max_catalog_options"] == 3
    assert policy["prefer_ready_stock"] is True
    assert policy["require_official_catalog_link"] is True
    assert policy["justify_recommendations"] is True


@pytest.mark.offline_eval
def test_persona_runtime_makes_recommendation_rules_executable():
    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    assert runtime.max_catalog_options == 3
    assert runtime.prefer_ready_stock is True
    assert runtime.require_qualification_before_catalog is True
    assert len(runtime.qualification_prompts) >= 2
    skills = runtime.sales_skills_block()
    assert "Qualificação" in skills
    assert "Recomendação" in skills
    assert "Objeções" in skills
    assert "max_catalog_options: 3" in runtime.interpreter_policy_block()


@pytest.mark.offline_eval
def test_customer_result_limit_reads_persona_runtime():
    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile={
            **_crono_chatbo_profile(),
            "recommendation_rules": ["Nunca apresentar mais de 2 peças de uma vez"],
        },
    )
    token = set_persona_runtime(runtime)
    try:
        assert customer_result_limit() == 2
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_prefer_ready_stock_reorders_shortlist():
    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        products = [
            {"id": "1", "name": "Sob encomenda", "category_id": "10"},
            {"id": "2", "name": "Pronto", "category_id": "403"},
        ]
        ordered = apply_persona_presentation_order(products)
        assert [p["id"] for p in ordered] == ["2", "1"]
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_golden_dourado_visor_preto_prefers_conjunction():
    from app.models import SalesInterpretation
    from app.preference_normalize import normalize_sales_interpretation
    from app.product_retrieval import prefer_dial_and_case_matches

    sales = normalize_sales_interpretation(
        SalesInterpretation(
            domain="commerce",
            goal="recommend",
            subject={"brand": "Bulova", "product_type": "relógio"},
            preferences={"color": "dourado"},
            references_previous_context=False,
            needs_clarification=False,
            confidence=0.9,
        ),
        message_text="quero bulova dourado com o visor preto",
    )
    assert sales.preferences.color == "preto"
    assert sales.preferences.material == "dourado"
    ranked = prefer_dial_and_case_matches(
        [
            {
                "id": "1",
                "brand": "Bulova",
                "name": "Bulova Marine Star automatico preto 96A288",
            },
            {
                "id": "2",
                "brand": "Bulova",
                "name": "Bulova Marine Star preto com dourado 98B278",
            },
        ],
        sales,
        limit=2,
    )
    assert ranked[0]["id"] == "2"


@pytest.mark.offline_eval
def test_golden_brand_only_seiko_requires_qualification():
    import app.sales_agent as sales_agent

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        interpretation = SalesInterpretation(
            domain="commerce",
            goal="find",
            subject={"product_type": "relógio", "brand": "Seiko"},
            preferences={},
            information_needed=["catalog"],
            references_previous_context=False,
            enough_information_to_search=True,
            ready_for_retrieval=True,
            stop_clarification=False,
            needs_clarification=False,
            confidence=0.95,
        )
        state = sales_agent._discovery_state(interpretation, [])
        assert state["persona_qualification_required"] is True
        assert state["force_retrieval"] is False
        assert state["qualification"]["ready"] is False
        assert state["qualification"]["has_brand"] is True
        assert "budget" in state["qualification"]["missing_dims"]
        question = sales_agent._persona_qualification_question(interpretation, state)
        assert question and "investimento" in question.casefold()
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_golden_brand_plus_budget_unlocks_catalog():
    import app.sales_agent as sales_agent

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        interpretation = SalesInterpretation(
            domain="commerce",
            goal="recommend",
            subject={"product_type": "relógio", "brand": "Seiko"},
            preferences={"budget_max": 5000},
            information_needed=["catalog"],
            references_previous_context=True,
            enough_information_to_search=True,
            ready_for_retrieval=True,
            stop_clarification=False,
            needs_clarification=False,
            confidence=0.95,
        )
        state = sales_agent._discovery_state(interpretation, [])
        assert state["persona_qualification_required"] is False
        assert state["qualification"]["ready"] is True
        assert state["qualification"]["satisfied_by"] == "brand+budget"
        assert state["force_retrieval"] is True
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_golden_brand_plus_style_unlocks_catalog():
    import app.sales_agent as sales_agent

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        interpretation = SalesInterpretation(
            domain="commerce",
            goal="recommend",
            subject={"brand": "Seiko"},
            preferences={"style": "mergulho"},
            information_needed=["catalog"],
            references_previous_context=True,
            enough_information_to_search=True,
            ready_for_retrieval=True,
            stop_clarification=False,
            needs_clarification=False,
            confidence=0.9,
        )
        state = sales_agent._discovery_state(interpretation, [])
        assert state["persona_qualification_required"] is False
        assert state["qualification"]["satisfied_by"] == "brand+style"
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_golden_sku_lock_skips_qualification():
    import app.sales_agent as sales_agent

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        interpretation = SalesInterpretation(
            domain="commerce",
            goal="find",
            subject={"brand": "Seiko", "model": "King Turtle", "reference": "SRPE05K1"},
            preferences={},
            information_needed=["catalog"],
            references_previous_context=False,
            enough_information_to_search=True,
            ready_for_retrieval=True,
            stop_clarification=False,
            needs_clarification=False,
            confidence=0.99,
        )
        state = sales_agent._discovery_state(interpretation, [])
        assert state["persona_qualification_required"] is False
        assert state["qualification"]["satisfied_by"] == "sku_lock"
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_golden_list_position_one_binds_to_latest_seiko_not_stale_tissot():
    seiko_list = [
        {
            "position": 1,
            "product_id": "1429",
            "name": "Seiko King Samurai",
            "brand": "Seiko",
            "reference": "SRPF79K1",
        },
        {
            "position": 2,
            "product_id": "1945",
            "name": "Seiko Automático",
            "brand": "Seiko",
            "reference": "SRPB55K1",
        },
        {
            "position": 3,
            "product_id": "1949",
            "name": "Seiko King Turtle",
            "brand": "Seiko",
            "reference": "SRPE05K1",
        },
    ]
    latest = {
        "last_presented_products": seiko_list,
        "product_resolution_state": "options_presented",
        "active_product": None,
        "pending_action": None,
    }
    donor = {
        "cart_session_id": "old-cart",
        "pending_action": "choose_checkout_channel",
        "active_product": {
            "product_id": "641",
            "name": "Tissot Seastar",
            "brand": "Tissot",
        },
        "last_presented_products": [
            {
                "position": 1,
                "product_id": "641",
                "name": "Tissot",
                "brand": "Tissot",
            }
        ],
    }
    merged = merge_commerce_states(latest, donor)
    state = CommerceConversationState.model_validate(merged)
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="inspect",
        subject={},
        preferences={},
        information_needed=["catalog"],
        references_previous_context=True,
        reference_type="list_position",
        reference_position=1,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        stop_clarification=False,
        needs_clarification=False,
        confidence=0.99,
    )
    ref, how = resolve_commerce_reference(interpretation, state)
    assert how == "product_id"
    assert ref is not None
    assert ref.product_id == "1429"
    assert "Seiko" in (ref.name or ref.brand or "")


def _looks_like_chronograph(product: dict) -> bool:
    import unicodedata

    name = str(product.get("name") or "")
    folded = unicodedata.normalize("NFKD", name.casefold())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return any(token in folded for token in ("cronografo", "chronograph", "chrono", "crono"))


def _catalog_pool_certina_and_others() -> list[dict]:
    return [
        {
            "id": "c1",
            "name": "Certina DS Action Diver Chrono Titânio",
            "brand": "Certina",
            "case_size": "44",
            "current_price": 9499,
            "available": True,
        },
        {
            "id": "c2",
            "name": "Certina DS Action Diver Automatico Titânio",
            "brand": "Certina",
            "case_size": "44",
            "current_price": 9199,
            "available": True,
        },
        {
            "id": "c3",
            "name": "Certina DS Action Titânio Cinza",
            "brand": "Certina",
            "case_size": "41",
            "current_price": 7899,
            "available": True,
        },
        {
            "id": "t1",
            "name": "Tissot PR 100 Cronógrafo Preto",
            "brand": "Tissot",
            "case_size": "41",
            "current_price": 4299,
            "available": True,
        },
        {
            "id": "s1",
            "name": "Seiko Chronograph Inox SSB313P1",
            "brand": "Seiko",
            "case_size": "42",
            "current_price": 4499,
            "available": True,
        },
        {
            "id": "b1",
            "name": "Bulova Cronógrafo Preto 96B336",
            "brand": "Bulova",
            "case_size": "42",
            "current_price": 3999,
            "available": True,
        },
    ]


@pytest.mark.offline_eval
def test_golden_joao_escape_certina_sticky_on_chrono_other_brands():
    """Contact 5548999490859 — must leave Certina when customer rejects it."""
    from app.preference_normalize import normalize_sales_interpretation
    from app.product_retrieval import hard_filter_products
    import app.sales_agent as sales_agent

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        sticky = SalesInterpretation(
            domain="commerce",
            goal="recommend",
            subject={"brand": "Certina", "product_type": "relógio"},
            preferences={
                "budget_max": 10000,
                "style": "mergulho",
                "material": "titânio",
                "color": "cinza",
            },
            information_needed=["catalog"],
            references_previous_context=True,
            enough_information_to_search=True,
            ready_for_retrieval=False,
            needs_clarification=True,
            confidence=0.9,
        )

        # Turn A: ask chronograph while stuck on Certina — must force search.
        chrono_ask = normalize_sales_interpretation(
            sticky.model_copy(deep=True),
            message_text="queria ver um crono tbm",
            context_text="queria um relógio de mergulho",
        )
        state_a = sales_agent._discovery_state(
            chrono_ask,
            [],
            message_text="queria ver um crono tbm",
        )
        assert state_a["force_retrieval"] is True
        assert state_a["wants_chronograph"] is True

        # Turn B: reject Certina explicitly — brand sticky must die.
        unlocked = normalize_sales_interpretation(
            sticky.model_copy(deep=True),
            message_text="agora quero um crono, não precisa ser certina",
            context_text="queria ver um crono tbm",
        )
        assert unlocked.subject.brand is None
        assert unlocked.ready_for_retrieval is True
        assert unlocked.stop_clarification is True
        assert any(
            str(item).lower().startswith("exclude_brand:certina")
            for item in (unlocked.preferences.attributes or [])
        )

        state_b = sales_agent._discovery_state(
            unlocked,
            [
                {
                    "role": "assistant",
                    "content": "Se quiser, eu já peço essa busca de crono Certina.",
                    "metadata": {"safety_reason": "commerce_clarification"},
                }
            ],
            message_text="agora quero um crono, não precisa ser certina",
        )
        assert state_b["force_retrieval"] is True
        assert state_b["brand_unlock_requested"] is True
        assert state_b["persona_qualification_required"] is False

        filtered = hard_filter_products(
            _catalog_pool_certina_and_others(),
            unlocked,
            mode="recommendation",
        )
        brands = {str(item.get("brand")) for item in filtered}
        assert "Certina" not in brands
        assert brands & {"Tissot", "Seiko", "Bulova"}
        assert filtered
        assert all(_looks_like_chronograph(item) for item in filtered)

        # Turn C: stronger rejection must still keep Certina out.
        rejected = normalize_sales_interpretation(
            unlocked.model_copy(deep=True),
            message_text="Não quero chrono da certina",
            context_text="outras opções de marca",
        )
        assert rejected.subject.brand is None
        filtered_c = hard_filter_products(
            _catalog_pool_certina_and_others(),
            rejected,
            mode="recommendation",
        )
        assert all(str(item.get("brand")) != "Certina" for item in filtered_c)
        assert filtered_c
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_golden_ricardo_escape_large_case_when_customer_sets_36_38():
    """Contact 5511937118008 — size ask must force retrieval and hard-filter mm."""
    from app.preference_normalize import normalize_sales_interpretation
    from app.product_retrieval import hard_filter_products
    import app.sales_agent as sales_agent

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        sticky = SalesInterpretation(
            domain="commerce",
            goal="recommend",
            subject={"brand": "Seiko", "product_type": "relógio"},
            preferences={"budget_min": 5000, "budget_max": 8000, "style": "versátil"},
            information_needed=["catalog"],
            references_previous_context=True,
            enough_information_to_search=False,
            ready_for_retrieval=False,
            needs_clarification=True,
            confidence=0.85,
        )
        sized = normalize_sales_interpretation(
            sticky,
            message_text="Me mande opções com tamanhos entre 36 até 38mm",
            context_text="Mais versátil. Tamanho menor pois meu pulso não é muito grande",
        )
        assert sized.ready_for_retrieval is True
        assert sized.stop_clarification is True
        state = sales_agent._discovery_state(
            sized,
            [],
            message_text="Me mande opções com tamanhos entre 36 até 38mm",
        )
        assert state["force_retrieval"] is True
        assert state["case_size_range"] == (36, 38)

        pool = [
            {
                "id": "sumo",
                "name": "Seiko Prospex Sumo",
                "brand": "Seiko",
                "case_size": "44",
                "current_price": 7599,
                "available": True,
            },
            {
                "id": "alpinist",
                "name": "Seiko Alpinist",
                "brand": "Seiko",
                "case_size": "38",
                "current_price": 6999,
                "available": True,
            },
            {
                "id": "prx",
                "name": "Tissot PRX 35mm",
                "brand": "Tissot",
                "case_size": "35",
                "current_price": 6500,
                "available": True,
            },
        ]
        # Stay on Seiko until customer unlocks brands; size filter still applies.
        filtered = hard_filter_products(pool, sized, mode="recommendation")
        assert {str(item["id"]) for item in filtered} == {"alpinist"}

        # Affirmation after size clarification must also force retrieval.
        affirm_state = sales_agent._discovery_state(
            sized,
            [
                {
                    "role": "assistant",
                    "content": "Quer que eu busque opções nessa faixa de 36 a 38 mm?",
                    "metadata": {"safety_reason": "commerce_clarification"},
                }
            ],
            message_text="Sim",
        )
        assert affirm_state["force_retrieval"] is True
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_golden_style_switch_diver_to_chrono_does_not_keep_false_diver_only():
    """Customer changing feature (diver → chrono) must re-rank to chronographs."""
    from app.preference_normalize import normalize_sales_interpretation
    from app.product_retrieval import hard_filter_products

    interpretation = normalize_sales_interpretation(
        SalesInterpretation(
            domain="commerce",
            goal="recommend",
            subject={"product_type": "relógio"},
            preferences={
                "budget_max": 10000,
                "style": "mergulho",
                "explicit_no_preferences": ["brand"],
            },
            information_needed=["catalog"],
            references_previous_context=True,
            enough_information_to_search=True,
            ready_for_retrieval=True,
            needs_clarification=False,
            confidence=0.9,
        ),
        message_text="agora quero um crono, não precisa ser certina",
        context_text="queria um relógio de mergulho",
    )
    assert interpretation.subject.brand is None
    filtered = hard_filter_products(
        _catalog_pool_certina_and_others(),
        interpretation,
        mode="recommendation",
    )
    assert filtered
    assert all(str(item.get("brand")) != "Certina" for item in filtered)
    assert all(_looks_like_chronograph(item) for item in filtered)


@pytest.mark.offline_eval
@pytest.mark.asyncio
async def test_golden_joao_close_buy_list_position_two_creates_cart(monkeypatch):
    """Contact 5548999490859 (31/08) — 'Quero comprar o 2' must create cart, not re-list."""
    from types import SimpleNamespace

    import app.sales_agent as sales_agent
    from app.commerce_context import CommerceConversationState
    from app.models import IncomingMessage
    from app.sales.purchase_selection import repair_presented_purchase_selection

    baltic_list = [
        {
            "position": 1,
            "product_id": "aq1",
            "name": "Baltic Aquascaphe",
            "brand": "Baltic",
        },
        {
            "position": 2,
            "product_id": "sb01",
            "name": "Baltic Classic SB01",
            "brand": "Baltic",
        },
        {
            "position": 3,
            "product_id": "mr01",
            "name": "Baltic MR01",
            "brand": "Baltic",
        },
    ]
    state = CommerceConversationState(
        active_domain="commerce",
        last_presented_products=baltic_list,
        purchase_stage="selection",
        product_resolution_state="options_presented",
    )
    # LLM wrongly re-opens discovery/recommendation instead of buying #2.
    wrong = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Baltic", "product_type": "relógio"},
        preferences={"style": "mergulho"},
        information_needed=["catalog"],
        references_previous_context=True,
        needs_clarification=False,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        confidence=0.9,
    )
    repaired = repair_presented_purchase_selection(
        wrong,
        message_text="Quero comprar o 2",
        state=state,
    )
    assert repaired is not None
    assert repaired.purchase_action == "create_cart"
    assert repaired.reference_position == 2

    calls: list[tuple[str, dict]] = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "sb01",
                "name": "Baltic Classic SB01",
                "current_price": "8900.00",
                "available": True,
                "has_variation": False,
            }
        if tool == "create_cart":
            return {
                "cart_id": "CART-JOAO",
                "session_id": "SESSION-JOAO",
                "cart_url": "https://loja.example/checkout/SESSION-JOAO",
            }
        if tool == "get_cart_complete":
            return {
                "cart_id": "CART-JOAO",
                "session_id": "SESSION-JOAO",
                "total": "8900.00",
                "items": [{"product_id": "sb01", "quantity": 1}],
            }
        raise AssertionError(f"unexpected tool {tool}")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="Quero comprar o 2"),
        {"primary_intent": "commerce"},
        {},
        wrong,
        commerce_state=state,
    )
    assert result is not None
    assert any(tool == "create_cart" for tool, _ in calls)
    create = next(args for tool, args in calls if tool == "create_cart")
    assert create["product_id"] == "sb01"
    assert result.response_metadata.get("purchase_stage") == "cart_created"
    # Must not re-list the three Baltic options as a fresh search.
    assert "Sim, encontrei" not in (result.reply_text or "")


@pytest.mark.offline_eval
@pytest.mark.asyncio
async def test_golden_joao_bare_quero_comprar_does_not_ask_name(monkeypatch):
    """Bare 'Quero comprar' with shortlist must not reopen persona qualification."""
    from types import SimpleNamespace

    import app.sales_agent as sales_agent
    from app.commerce_context import CommerceConversationState
    from app.models import IncomingMessage
    from app.persona_runtime import (
        build_persona_runtime,
        reset_persona_runtime,
        set_persona_runtime,
    )

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        state = CommerceConversationState(
            active_domain="commerce",
            last_presented_products=[
                {
                    "position": 1,
                    "product_id": "aq1",
                    "name": "Baltic Aquascaphe",
                    "brand": "Baltic",
                },
                {
                    "position": 2,
                    "product_id": "sb01",
                    "name": "Baltic Classic SB01",
                    "brand": "Baltic",
                },
                {
                    "position": 3,
                    "product_id": "mr01",
                    "name": "Baltic MR01",
                    "brand": "Baltic",
                },
            ],
            purchase_stage="selection",
        )
        wrong = SalesInterpretation(
            domain="commerce",
            goal="discover",
            subject={"product_type": "relógio"},
            preferences={},
            information_needed=["catalog"],
            references_previous_context=True,
            needs_clarification=True,
            clarification_question="Como posso te chamar?",
            enough_information_to_search=False,
            ready_for_retrieval=False,
            confidence=0.8,
        )
        monkeypatch.setattr(
            sales_agent,
            "get_settings",
            lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
        )

        result = await sales_agent.handle_sales_message(
            IncomingMessage(text="Quero comprar"),
            {"primary_intent": "commerce"},
            {},
            wrong,
            commerce_state=state,
            recent_turns=[],
        )
        assert result is not None
        reply = (result.reply_text or "").casefold()
        assert "como posso te chamar" not in reply
        assert "florian" not in reply
        assert "ocasi" not in reply
        assert "orçamento" not in reply and "orcamento" not in reply
        assert "qual" in reply and ("opção" in reply or "opcao" in reply or "1" in reply)
    finally:
        reset_persona_runtime(token)
