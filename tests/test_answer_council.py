import pytest

from app.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.sales.answer_council import (
    apply_corrections,
    apply_turn_contract_for_search,
    build_turn_contract,
    check_fatos,
    check_pedido,
    judge_council,
)
from tests.test_tray_query_authority import OMEGA_OVER_BUDGET, _interpretation


def _omega_draft() -> AgentResult:
    return AgentResult(
        reply_text=(
            "Não fechei a combinação exata, mas estes Omega mais próximos "
            "batem com o que você pediu."
        ),
        intent="commerce",
        commercial_data={"products": OMEGA_OVER_BUDGET},
        response_metadata={
            "presented_products": True,
            "guided_near_match": True,
            "hard_budget_max": 5000,
        },
    )


def test_contract_nessa_faixa_keeps_memory_budget_and_omega_brand():
    interp = _interpretation()
    state = CommerceConversationState(
        active_preferences={"budget": {"max": 5000}, "occasion": {"value": "trabalho"}}
    )
    contract = build_turn_contract(
        message_text="tem algum omega nessa faixa de preço?",
        interpretation=interp,
        commerce_state=state,
    )
    assert contract.brand == "Omega"
    assert contract.budget_max == 5000
    assert contract.asks_price_range is True
    assert contract.must_not_claim_stale_occasion is True


def test_checker_a_rejects_over_budget_near_match():
    contract = build_turn_contract(
        message_text="tem algum omega nessa faixa de preço?",
        interpretation=_interpretation(),
        commerce_state=CommerceConversationState(
            active_preferences={"budget": {"max": 5000}}
        ),
    )
    report = check_pedido(_omega_draft(), contract)
    assert report.pass_check is False
    assert "presented_over_budget" in report.issues
    assert "near_match_with_budget" in report.issues


def test_checker_b_rejects_fact_price_over_budget():
    contract = build_turn_contract(
        message_text="tem algum omega nessa faixa de preço?",
        interpretation=_interpretation(),
        commerce_state=None,
    )
    report = check_fatos(_omega_draft(), contract)
    assert report.pass_check is False
    assert "fact_price_over_budget" in report.issues


def test_checker_a_rejects_stale_trabalho_claim():
    interp = _interpretation(preferences={"budget_max": 5000, "occasion": "trabalho"})
    contract = build_turn_contract(
        message_text="quero um relogio",
        interpretation=interp,
        commerce_state=CommerceConversationState(
            active_preferences={"occasion": {"value": "trabalho"}}
        ),
    )
    assert contract.must_not_claim_stale_occasion is True
    draft = AgentResult(
        reply_text="Tironi, para trabalho eu separaria estas 3 opções.",
        intent="commerce",
        commercial_data={"products": []},
    )
    report = check_pedido(draft, contract)
    assert "stale_occasion_claimed" in report.issues


def test_judge_requires_both_checkers_and_allows_one_restart():
    fail = check_pedido(
        _omega_draft(),
        build_turn_contract(
            message_text="tem algum omega nessa faixa de preço?",
            interpretation=_interpretation(),
            commerce_state=None,
        ),
    )
    ok = check_fatos(
        AgentResult(reply_text="ok", intent="commerce", commercial_data={"products": []}),
        build_turn_contract(
            message_text="oi",
            interpretation=_interpretation(preferences={}),
            commerce_state=None,
        ),
    )
    decision = judge_council(fail, ok, attempt=1, max_restarts=1)
    assert decision.approved is False
    assert decision.restart is True
    decision2 = judge_council(fail, ok, attempt=2, max_restarts=1)
    assert decision2.restart is False


def test_corrections_drop_stale_occasion_and_enforce_budget():
    interp = _interpretation(preferences={"budget_max": None, "occasion": "trabalho"})
    contract = build_turn_contract(
        message_text="tem algum omega nessa faixa de preço?",
        interpretation=_interpretation(),
        commerce_state=CommerceConversationState(
            active_preferences={"budget": {"max": 5000}}
        ),
    )
    updated = apply_corrections(
        interp,
        contract,
        ["enforce_budget", "drop_stale_occasion", "forbid_near_match"],
    )
    assert updated.preferences.budget_max == 5000
    assert updated.preferences.occasion is None
    assert updated._forbid_near_match is True
    assert updated._force_recommendation_mode is True


def test_corrections_continue_commerce_leaves_greeting_domain():
    interp = SalesInterpretation(
        domain="greeting",
        goal=None,
        subject={},
        preferences={},
        information_needed=[],
        references_previous_context=False,
        enough_information_to_search=False,
        ready_for_retrieval=False,
        stop_clarification=False,
        needs_clarification=False,
        clarification_question=None,
        confidence=0.9,
    )
    contract = build_turn_contract(
        message_text="quero um relogio",
        interpretation=_interpretation(),
        commerce_state=None,
    )
    updated = apply_corrections(interp, contract, ["continue_commerce"])
    assert updated.domain == "commerce"
    assert updated.goal == "discover"
    assert updated.subject.product_type == "relógio"
    assert updated.references_previous_context is True


def test_contract_live_shortlist_blocks_regreet():
    from app.commerce_context import PresentedCommerceProduct

    contract = build_turn_contract(
        message_text="Qual relógio?",
        interpretation=_interpretation(preferences={}),
        commerce_state=CommerceConversationState(
            last_presented_products=[
                PresentedCommerceProduct(
                    product_id="1", name="Seiko 5 Sports", position=1
                )
            ]
        ),
    )
    assert contract.live_shortlist is True
    assert contract.must_not_re_greet is True


def test_contract_purchase_close_locks_sku():
    from app.commerce_context import PresentedCommerceProduct

    contract = build_turn_contract(
        message_text="quero comprar o 2",
        interpretation=_interpretation(brand="Baltic", preferences={}),
        commerce_state=CommerceConversationState(
            last_presented_products=[
                PresentedCommerceProduct(product_id="101", name="A", position=1),
                PresentedCommerceProduct(product_id="202", name="B", position=2),
            ]
        ),
    )
    assert contract.purchase_close is True
    assert contract.sku_lock is True
    assert contract.must_not_re_greet is True


def test_checker_rejects_relist_on_purchase_close():
    from app.commerce_context import PresentedCommerceProduct

    contract = build_turn_contract(
        message_text="quero comprar o 2",
        interpretation=_interpretation(brand="Baltic", preferences={}),
        commerce_state=CommerceConversationState(
            last_presented_products=[
                PresentedCommerceProduct(product_id="101", name="A", position=1),
                PresentedCommerceProduct(product_id="202", name="B", position=2),
            ]
        ),
    )
    draft = AgentResult(
        reply_text="Separei 3 opções Baltic pra você.",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "101", "name": "A", "brand": "Baltic"},
                {"id": "202", "name": "B", "brand": "Baltic"},
                {"id": "303", "name": "C", "brand": "Baltic"},
            ]
        },
    )
    report = check_pedido(draft, contract)
    assert "reopened_discovery_on_purchase_close" in report.issues
    decision = judge_council(
        report,
        check_fatos(draft, contract),
        attempt=1,
        max_restarts=1,
    )
    assert "honor_sku_lock" in decision.correction_codes


def test_checker_rejects_name_question_after_shortlist():
    from app.commerce_context import PresentedCommerceProduct

    contract = build_turn_contract(
        message_text="o 2",
        interpretation=_interpretation(),
        commerce_state=CommerceConversationState(
            last_presented_products=[
                PresentedCommerceProduct(product_id="1", name="Seiko", position=1)
            ]
        ),
    )
    draft = AgentResult(
        reply_text="Como posso te chamar?",
        intent="commerce",
        commercial_data={"products": []},
    )
    report = check_pedido(draft, contract)
    assert "requalify_after_sku" in report.issues


def test_bare_oi_without_live_sale_may_greet():
    contract = build_turn_contract(
        message_text="oi",
        interpretation=_interpretation(preferences={}),
        commerce_state=None,
    )
    assert contract.must_not_re_greet is False


@pytest.mark.asyncio
async def test_council_restart_applies_forbid_near_match(monkeypatch):
    from app.sales.answer_council import apply_answer_council_with_retry
    from app.sales.tray_query_authority import budget_hard_miss_result

    captured: dict[str, object] = {}

    async def fake_retrieval(interp, message_text=None, commerce_state=None):
        captured["forbid"] = bool(getattr(interp, "_forbid_near_match", False))
        captured["budget"] = interp.preferences.budget_max
        return budget_hard_miss_result(interp, OMEGA_OVER_BUDGET)

    monkeypatch.setattr(
        "app.sales.product_lookup.execute_compiled_product_retrieval",
        fake_retrieval,
    )
    incoming = IncomingMessage(
        channel="whatsapp",
        text="tem algum omega nessa faixa de preço?",
    )
    result, decision, _interp = await apply_answer_council_with_retry(
        _omega_draft(),
        incoming=incoming,
        interpretation=_interpretation(),
        commerce_state=CommerceConversationState(
            active_preferences={"budget": {"max": 5000}}
        ),
    )
    assert captured.get("forbid") is True
    assert captured.get("budget") == 5000
    assert "mais próximos" not in (result.reply_text or "")
    assert (result.commercial_data or {}).get("products") == []
    assert decision.attempts >= 1


@pytest.mark.asyncio
async def test_council_continue_commerce_skips_catalog_search(monkeypatch):
    from app.commerce_context import PresentedCommerceProduct
    from app.greeting_policy import GREETING_REPLY
    from app.sales.answer_council import apply_answer_council_with_retry

    called = {"n": 0}

    async def fake_retrieval(*_args, **_kwargs):
        called["n"] += 1
        return None

    monkeypatch.setattr(
        "app.sales.product_lookup.execute_compiled_product_retrieval",
        fake_retrieval,
    )
    incoming = IncomingMessage(channel="whatsapp", text="quero um relogio")
    result, _decision, _interp = await apply_answer_council_with_retry(
        AgentResult(reply_text=GREETING_REPLY, intent="commerce"),
        incoming=incoming,
        interpretation=_interpretation(),
        commerce_state=CommerceConversationState(
            last_presented_products=[
                PresentedCommerceProduct(
                    product_id="55",
                    name="Seiko Presage",
                    position=1,
                )
            ]
        ),
    )
    assert called["n"] == 0
    assert "Olá" not in (result.reply_text or "")
    assert "Seiko Presage" in (result.reply_text or "")
    assert result.response_metadata.get("answer_council_continue") is True


def test_first_search_binds_memory_budget_and_forbids_near_match():
    interp = _interpretation(preferences={"budget_max": None, "occasion": "trabalho"})
    bound = apply_turn_contract_for_search(
        interp,
        message_text="tem algum omega nessa faixa de preço?",
        commerce_state=CommerceConversationState(
            active_preferences={
                "budget": {"max": 5000},
                "occasion": {"value": "trabalho"},
            }
        ),
    )
    assert bound.preferences.budget_max == 5000
    assert bound.preferences.occasion is None
    assert bound._forbid_near_match is True
    assert bound.subject.brand == "Omega"


def test_first_search_drops_stale_budget_on_open_browse():
    interp = _interpretation(
        preferences={"budget_max": 5000, "occasion": "trabalho"}
    )
    bound = apply_turn_contract_for_search(
        interp,
        message_text="quero um relogio",
        commerce_state=CommerceConversationState(
            active_preferences={
                "budget": {"max": 5000},
                "occasion": {"value": "trabalho"},
            }
        ),
    )
    assert bound.preferences.budget_max is None
    assert bound.preferences.occasion is None
    assert bound._forbid_near_match is False


def test_first_search_clears_commerce_phrase_used_as_name():
    interp = _interpretation(
        preferences={
            "budget_max": 5000,
            "recipient": "quero um relogio",
            "attributes": ["qual:name:quero um relogio"],
        }
    )
    bound = apply_turn_contract_for_search(
        interp,
        message_text="tem algum omega nessa faixa de preço?",
        commerce_state=CommerceConversationState(
            active_preferences={"budget": {"max": 5000}}
        ),
    )
    assert bound.preferences.recipient is None
    assert "qual:name:" not in " ".join(bound.preferences.attributes or [])


def test_first_search_does_not_unpause_purchase_close():
    from app.commerce_context import PresentedCommerceProduct

    interp = _interpretation(brand="Baltic", preferences={})
    interp = interp.model_copy(
        update={
            "goal": "buy",
            "purchase_action": "create_cart",
            "ready_for_retrieval": False,
        }
    )
    bound = apply_turn_contract_for_search(
        interp,
        message_text="quero comprar o 2",
        commerce_state=CommerceConversationState(
            last_presented_products=[
                PresentedCommerceProduct(product_id="1", name="A", position=1),
                PresentedCommerceProduct(product_id="2", name="B", position=2),
            ]
        ),
    )
    assert bound.ready_for_retrieval is False
    assert bound.goal == "buy"


def test_contract_locks_color_gender_style_from_this_message():
    contract = build_turn_contract(
        message_text="quero um seiko azul feminino esportivo",
        interpretation=_interpretation(brand="Seiko", preferences={}),
        commerce_state=None,
    )
    assert contract.color == "azul"
    assert contract.gender == "feminino"
    assert contract.style == "esportivo"
    assert contract.color_from_this_message is True
    assert "color_lock" in contract.hard_codes


def test_open_browse_drops_stale_color_and_style():
    interp = _interpretation(
        brand="Seiko",
        preferences={"color": "preto", "style": "esportivo"},
    )
    bound = apply_turn_contract_for_search(
        interp,
        message_text="quero um relogio",
        commerce_state=CommerceConversationState(
            active_preferences={"color": "preto", "style": "esportivo"}
        ),
    )
    assert bound.preferences.color is None
    assert bound.preferences.style is None


def test_first_search_binds_stated_azul():
    interp = _interpretation(brand="Seiko", preferences={})
    bound = apply_turn_contract_for_search(
        interp,
        message_text="quero um seiko azul",
        commerce_state=None,
    )
    assert bound.preferences.color == "azul"


def test_checker_rejects_rival_dial_color_when_asked_azul():
    contract = build_turn_contract(
        message_text="quero um seiko azul",
        interpretation=_interpretation(brand="Seiko", preferences={"color": "azul"}),
        commerce_state=None,
    )
    draft = AgentResult(
        reply_text="Separei estas opções Seiko.",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "1", "name": "Seiko 5 Sports Black", "brand": "Seiko"},
                {"id": "2", "name": "Seiko Presage Black", "brand": "Seiko"},
            ]
        },
    )
    report = check_pedido(draft, contract)
    assert "ignored_color" in report.issues
    facts = check_fatos(draft, contract)
    assert "fact_color_mismatch" in facts.issues
    decision = judge_council(report, facts, attempt=1, max_restarts=1)
    assert "enforce_color" in decision.correction_codes
    assert "forbid_near_match" in decision.correction_codes


def test_checker_allows_list_without_color_word():
    contract = build_turn_contract(
        message_text="quero um seiko azul",
        interpretation=_interpretation(brand="Seiko", preferences={"color": "azul"}),
        commerce_state=None,
    )
    draft = AgentResult(
        reply_text="Separei estas opções Seiko.",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "1", "name": "Seiko Presage Automatic", "brand": "Seiko"},
            ]
        },
    )
    report = check_pedido(draft, contract)
    assert "ignored_color" not in report.issues


def test_checker_rejects_masculino_titles_when_asked_feminino():
    contract = build_turn_contract(
        message_text="quero um relogio feminino",
        interpretation=_interpretation(
            preferences={"recipient": "feminino", "attributes": ["feminino"]}
        ),
        commerce_state=None,
    )
    assert contract.gender == "feminino"
    draft = AgentResult(
        reply_text="Separei estas opções.",
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": "1",
                    "name": "Relógio Masculino Seiko 5 Sports",
                    "brand": "Seiko",
                }
            ]
        },
    )
    report = check_pedido(draft, contract)
    assert "ignored_gender" in report.issues


def test_nessa_faixa_inherits_memory_color():
    interp = _interpretation(
        brand="Seiko",
        preferences={"budget_max": 5000, "color": "azul"},
    )
    contract = build_turn_contract(
        message_text="tem algum seiko nessa faixa de preço?",
        interpretation=interp,
        commerce_state=CommerceConversationState(
            active_preferences={"budget": {"max": 5000}, "color": "azul"}
        ),
    )
    assert contract.asks_price_range is True
    assert contract.color == "azul"
    assert "color" not in contract.stale_fields


def _tissot_cart_state(**extra) -> CommerceConversationState:
    payload = {
        "cart_session_id": "tissot-leftover",
        "cart_url": "https://loja.example/checkout/tissot",
        "pending_action": "choose_checkout_channel",
        "purchase_stage": "cart_created",
        "dialogue_phase": "checkout",
        "active_product": {
            "product_id": "t1",
            "name": "Tissot PRX",
            "brand": "Tissot",
        },
    }
    payload.update(extra)
    return CommerceConversationState(**payload)


def test_open_browse_marks_leftover_cart_stale():
    contract = build_turn_contract(
        message_text="quero um relogio",
        interpretation=_interpretation(brand=None, goal="discover", preferences={}),
        commerce_state=_tissot_cart_state(),
    )
    assert "checkout" in contract.stale_fields
    assert contract.live_checkout is False
    assert contract.must_not_claim_stale_checkout is True


def test_greeting_keeps_live_checkout():
    contract = build_turn_contract(
        message_text="oi",
        interpretation=_interpretation(brand=None, goal="buy", preferences={}),
        commerce_state=_tissot_cart_state(),
    )
    assert "checkout" not in contract.stale_fields
    assert contract.live_checkout is True
    assert contract.must_not_re_greet is True
    assert contract.must_not_claim_stale_checkout is False


def test_purchase_close_does_not_drop_checkout():
    from app.commerce_context import PresentedCommerceProduct

    contract = build_turn_contract(
        message_text="quero comprar o 2",
        interpretation=_interpretation(brand="Tissot", goal="buy", preferences={}),
        commerce_state=_tissot_cart_state(
            last_presented_products=[
                PresentedCommerceProduct(product_id="1", name="A", position=1),
                PresentedCommerceProduct(product_id="2", name="B", position=2),
            ]
        ),
    )
    assert contract.purchase_close is True
    assert "checkout" not in contract.stale_fields
    assert contract.live_checkout is True


def test_checker_rejects_cart_prompt_on_open_browse():
    contract = build_turn_contract(
        message_text="quero um relogio",
        interpretation=_interpretation(brand=None, goal="discover", preferences={}),
        commerce_state=_tissot_cart_state(),
    )
    draft = AgentResult(
        reply_text=(
            "Seu carrinho está pronto. Prefere fechar por aqui no WhatsApp "
            "ou continuar pelo site?"
        ),
        intent="commerce",
        commercial_data={"cart": {"status": "ready"}},
    )
    report = check_pedido(draft, contract)
    assert "claimed_stale_checkout" in report.issues
    facts = check_fatos(draft, contract)
    assert "fact_stale_checkout" in facts.issues
    decision = judge_council(report, facts, attempt=1, max_restarts=1)
    assert "drop_stale_checkout" in decision.correction_codes


def test_first_search_drops_stale_checkout_purchase_action():
    interp = _interpretation(brand=None, goal="buy", preferences={})
    interp.purchase_action = "create_cart"
    bound = apply_turn_contract_for_search(
        interp,
        message_text="quero um relogio",
        commerce_state=_tissot_cart_state(),
    )
    assert bound.purchase_action is None
    assert bound.goal == "discover"


def test_evolve_clears_leftover_cart_after_stale_checkout_stamp():
    from app.commerce_context import evolve_commerce_state
    from app.cart_service import _clear_cart_session_state

    previous = _tissot_cart_state()
    result = AgentResult(
        reply_text="Qual faixa de investimento você tem em mente?",
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "dialogue_phase": "discovery",
            "dialogue_phase_reset": True,
            "clear_pending_action": True,
            "clear_active_product": True,
            "cart_state": _clear_cart_session_state(previous),
        },
    )
    evolved = evolve_commerce_state(previous, result)
    assert evolved.cart_session_id is None
    assert evolved.pending_action is None
    assert evolved.active_product is None
    assert evolved.dialogue_phase == "discovery"


def test_mk2_plus_color_does_not_force_recommendation():
    from app.preference_normalize import normalize_sales_interpretation

    interp = normalize_sales_interpretation(
        _interpretation(brand="Baltic", model=None, preferences={"color": "cinza"}),
        message_text="quero o mk2 cinza 37mm",
    )
    bound = apply_turn_contract_for_search(
        interp,
        message_text="quero o mk2 cinza 37mm",
        commerce_state=None,
    )
    assert bound._forbid_near_match is True
    assert bound._force_recommendation_mode is False
    assert "mk2" in (bound.subject.model or "").casefold()


def test_color_refinement_keeps_presented_mk2():
    from app.commerce_context import PresentedCommerceProduct
    from app.preference_normalize import normalize_sales_interpretation

    interp = normalize_sales_interpretation(
        _interpretation(brand=None, model=None, preferences={"color": "cinza"}),
        message_text="quero o cinza com caixa de 37mm",
    )
    state = CommerceConversationState(
        last_presented_products=[
            PresentedCommerceProduct(
                product_id="1",
                name="Relógio Baltic Aquascaphe MK2 Automático Prata",
                brand="Baltic",
                position=1,
            )
        ],
        active_preferences={
            "locked_identity": {"brand": "Baltic", "model": "Aquascaphe mk2"}
        },
    )
    contract = build_turn_contract(
        message_text="quero o cinza com caixa de 37mm",
        interpretation=interp,
        commerce_state=state,
    )
    assert contract.sku_lock is True
    assert "mk2" in (contract.model or "").casefold()
    bound = apply_turn_contract_for_search(
        interp,
        message_text="quero o cinza com caixa de 37mm",
        commerce_state=state,
    )
    assert bound._force_recommendation_mode is False
    assert "mk2" in (bound.subject.model or "").casefold()
    assert (bound.subject.brand or "").casefold() == "baltic"


def test_checker_rejects_hermetique_when_mk2_locked():
    contract = build_turn_contract(
        message_text="quero o mk2 cinza 37mm",
        interpretation=_interpretation(
            brand="Baltic",
            model="Aquascaphe mk2",
            preferences={"color": "cinza"},
        ),
        commerce_state=None,
    )
    draft = AgentResult(
        reply_text="Encontrei: 1. Relógio Baltic Hermétique Tourer...",
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": "9",
                    "name": "Relógio Baltic Hermétique Tourer Azul",
                    "brand": "Baltic",
                }
            ]
        },
    )
    report = check_pedido(draft, contract)
    assert "ignored_model" in report.issues


def test_purchase_close_cart_sku_skips_model_mismatch():
    interp = _interpretation(brand="Marca", model="Modelo", preferences={})
    interp = interp.model_copy(update={"purchase_action": "create_cart", "goal": "buy"})
    contract = build_turn_contract(
        message_text="compra direta de produto",
        interpretation=interp,
        commerce_state=None,
    )
    assert contract.purchase_close is True
    draft = AgentResult(
        reply_text="Carrinho pronto.",
        intent="commerce",
        commercial_data={
            "cart": {"status": "created", "session_id": "SESSION-1"},
            "products": [{"id": "P1", "name": "Produto P1", "brand": "Marca"}],
        },
    )
    assert "ignored_model" not in check_pedido(draft, contract).issues
    assert "fact_model_mismatch" not in check_fatos(draft, contract).issues
