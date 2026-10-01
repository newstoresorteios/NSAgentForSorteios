"""Regressions from the 2026-10-01 19:58 voice feedback; no live model calls."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.commerce.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.sales.responder import sales_response_with_openai


PRODUCT = {"id": "12", "name": "Relógio Exemplo Clássico 38mm", "price": 2500,
           "url": "https://www.newstorerj.com.br/exemplo", "_revalidated": True}


def interpretation(**changes):
    return SalesInterpretation(**{
        "domain": "commerce", "goal": "find", "references_previous_context": False,
        "needs_clarification": False, "confidence": .95, **changes,
    })


@pytest.fixture
def generation(monkeypatch):
    import app.sales_agent as sales
    monkeypatch.setattr(sales, "get_settings", lambda: SimpleNamespace(
        openai_api_key="offline-test", openai_model="offline-test"))
    monkeypatch.setattr("app.llm.prompt_compiler.resolve_system_instructions",
                        lambda **kw: kw["fallback_instructions"])
    mock = AsyncMock(return_value=SimpleNamespace(text="Para o casamento, veja o Relógio Exemplo Clássico 38mm."))
    monkeypatch.setattr("app.llm.openai_gateway.generate_text_output", mock)
    return mock


@pytest.mark.asyncio
async def test_first_catalog_request_with_occasion_reaches_the_model(generation):
    result = await sales_response_with_openai(
        IncomingMessage(text="Tenho um casamento semana que vem. Quais relógios vocês têm?"),
        {"intent": "product_search", "goal": "find"},
        AgentResult(reply_text="Lista: Exemplo", commercial_data={"products": [PRODUCT]},
                    response_metadata={"presented_products": True}),
        interpretation(preferences={"occasion": "casamento", "delivery_deadline_text": "semana que vem"}),
        recent_turns=[],
    )
    generation.assert_awaited_once()
    payload = json.loads(generation.call_args.kwargs["messages"][-1]["content"])
    assert payload["SALES_CONVERSATION"]["known_preferences"]["occasion"] == "casamento"
    assert payload["SALES_CONVERSATION"]["known_preferences"]["delivery_deadline_text"] == "semana que vem"
    assert result.response_metadata["used_openai_responder"] is True


@pytest.mark.asyncio
async def test_short_followup_keeps_motive_and_does_not_reask_known_slots(generation):
    await sales_response_with_openai(
        IncomingMessage(text="o azul"), {"intent": "product_search", "goal": "find"},
        AgentResult(reply_text="Lista", commercial_data={"products": [PRODUCT]}),
        interpretation(preferences={"color": "azul"}, references_previous_context=True),
        state=CommerceConversationState(active_preferences={"occasion": "casamento", "budget_max": 3000,
              "delivery_deadline_text": "semana que vem", "explicit_no_preferences": ["brand"]}),
        recent_turns=[{"role": "user", "content": "É para um casamento; até 3000."}],
    )
    payload = json.loads(generation.call_args.kwargs["messages"][-1]["content"])
    brief = payload["SALES_CONVERSATION"]
    assert brief["known_preferences"]["occasion"] == "casamento"
    assert brief["known_preferences"]["color"] == "azul"
    assert {"occasion", "budget", "brand", "color"} <= set(brief["do_not_ask_again"])


@pytest.mark.asyncio
async def test_bare_exact_lookup_keeps_fast_copy(generation):
    facts = AgentResult(reply_text="Relógio Exemplo Clássico 38mm", commercial_data={"products": [PRODUCT]})
    result = await sales_response_with_openai(IncomingMessage(text="REF12345"),
        {"intent": "product_search", "goal": "find"}, facts,
        interpretation(subject={"reference": "REF12345"}), recent_turns=[])
    generation.assert_not_awaited()
    assert result.reply_text == facts.reply_text


@pytest.mark.asyncio
async def test_multi_option_search_gets_a_conversation_not_just_a_dump(generation):
    await sales_response_with_openai(IncomingMessage(text="Quais relógios têm?"),
        {"intent": "product_search", "goal": "find"},
        AgentResult(reply_text="Lista", commercial_data={"products": [PRODUCT, {**PRODUCT, "id": "13"}]}),
        interpretation(), recent_turns=[])
    generation.assert_awaited_once()


def test_new_objective_does_not_import_previous_occasion():
    from app.sales.consultative_response import sales_conversation_brief
    brief = sales_conversation_brief(interpretation(preferences={"occasion": "academia"}),
        CommerceConversationState(active_preferences={"occasion": "casamento", "budget_max": 3000}))
    assert brief["known_preferences"] == {"occasion": "academia"}


def test_known_brand_and_wrist_size_are_not_new_discovery_questions():
    from app.sales.consultative_response import sales_conversation_brief
    brief = sales_conversation_brief(interpretation(subject={'brand': 'Tissot'},
        preferences={'attributes': ['case_size:38-38mm']}), None)
    assert {'brand', 'case_size'} <= set(brief['do_not_ask_again'])
    assert brief['known_subject'] == {'brand': 'Tissot'}


def test_removed_brand_does_not_return_through_prior_subject():
    from app.sales.consultative_response import sales_conversation_brief
    brief = sales_conversation_brief(interpretation(references_previous_context=True,
        preferences={'explicit_no_preferences': ['brand']}),
        CommerceConversationState(active_preferences={'subject_brand': 'Tissot'}))
    assert brief['known_subject'] == {}


def test_new_brand_choice_replaces_prior_indifference():
    from app.sales.consultative_response import sales_conversation_brief
    brief = sales_conversation_brief(interpretation(references_previous_context=True,
        subject={'brand': 'Tissot'}),
        CommerceConversationState(active_preferences={'explicit_no_preferences': ['brand']}))
    assert brief['known_subject'] == {'brand': 'Tissot'}


@pytest.mark.asyncio
async def test_budget_failure_keeps_grounded_result_available(generation):
    from app.ops.turn_runtime import LLMCallBudgetExceeded
    generation.side_effect = LLMCallBudgetExceeded("response_composition")
    result = await sales_response_with_openai(IncomingMessage(text="Para um casamento"),
        {"intent": "product_search", "goal": "find"},
        AgentResult(reply_text="Lista", commercial_data={"products": [PRODUCT]}),
        interpretation(preferences={"occasion": "casamento"}), recent_turns=[])
    assert result is None  # The caller retains its verified catalog fallback.


@pytest.fixture
def ready(monkeypatch):
    from app.persona.persona_runtime import PersonaRuntimeConfig, set_persona_runtime, reset_persona_runtime
    token = set_persona_runtime(PersonaRuntimeConfig(loaded=True, enabled=True, workspace_id="shop"))
    monkeypatch.setattr("app.sales.ready_delivery.enabled", lambda: True)
    mock = AsyncMock(return_value={"success": True, "complete": True, "products": [
        {"name": "Relógio Exemplo Clássico 38mm", "url": "https://www.newstorerj.com/exemplo",
         "listedAvailable": True, "price": 999, "product_id": "foreign-id"}]})
    monkeypatch.setattr("app.tray.tray_adapter_client.TrayAdapterClient.search_ready_delivery", mock)
    yield mock
    reset_persona_runtime(token)


def incoming(text):
    return IncomingMessage(text=text, channel="whatsapp", sender_key="customer", conversation_id="thread")


@pytest.mark.asyncio
async def test_wedding_and_deadline_are_not_catalog_search_tokens(ready):
    from app.sales.ready_delivery import try_ready_delivery
    result = await try_ready_delivery(incoming("Tenho um casamento semana que vem, o que tem à pronta entrega?"),
        interpretation=interpretation(preferences={"occasion": "casamento", "delivery_deadline_text": "semana que vem",
                                                   "delivery_mode": "ready_to_ship"}), recent_turns=[])
    ready.assert_awaited_once_with("pronta entrega")
    assert "casamento" in result.reply_text
    assert "https://www.newstorerj.com/pronta-entrega" in result.reply_text
    assert "comercial" not in result.reply_text
    assert not result.response_metadata.get("handoff", {}).get("offer")
    assert not result.commercial_data
    assert "foreign-id" not in str(result.response_metadata)


@pytest.mark.asyncio
async def test_specific_ready_lookup_retains_model_color_and_size(ready):
    from app.sales.ready_delivery import try_ready_delivery
    await try_ready_delivery(incoming("Tissot PRX azul 35mm para casamento semana que vem, pronta entrega?"),
        interpretation=interpretation(subject={"brand": "Tissot", "model": "PRX"},
            preferences={"color": "azul", "occasion": "casamento", "delivery_deadline_text": "semana que vem"}),
        recent_turns=[])
    query = ready.call_args.args[0].casefold()
    assert all(term in query for term in ["tissot", "prx", "azul", "35mm"])
    assert "casamento" not in query and "semana" not in query


@pytest.mark.asyncio
async def test_ready_delivery_can_use_model_without_importing_other_store_facts(ready, generation):
    from app.sales.ready_delivery import try_ready_delivery
    generation.return_value.text = ("Para o casamento, vamos olhar as opções da lista.\n"
        "Relógio Exemplo Clássico 38mm\nhttps://www.newstorerj.com/exemplo\n"
        "Catálogo: https://www.newstorerj.com/pronta-entrega\n"
        "O estoque final e a entrega no prazo precisam ser confirmados antes de fechar.")
    result = await try_ready_delivery(incoming("Pronta entrega para casamento?"),
        interpretation=interpretation(preferences={"occasion": "casamento"}), recent_turns=[])
    generation.assert_awaited_once()
    assert result.response_metadata["used_openai_responder"] is True
    assert result.response_metadata["response_source"] == "ready_delivery_storefront"
    assert result.commercial_data is None
    payload = json.loads(generation.call_args.kwargs["messages"][-1]["content"])
    assert payload["FACTS"]["stock_confirmed"] is False
    assert "price" not in payload["FACTS"]["products"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_copy", [
    "Custa R$ 999. https://www.newstorerj.com/exemplo",
    "Veja https://www.newstorerj.com.br/outro",
    "Encontrei boas opções para você.",
    "https://www.newstorerj.com/exemplo",
    "Relógio Exemplo Clássico 38mm https://www.newstorerj.com/exemplo. Chega antes do casamento.",
    "Relógio Exemplo Clássico 38mm https://www.newstorerj.com/exemplo. Temos em estoque.",
    "Relógio Exemplo Clássico 38mm https://www.newstorerj.com/exemplo. Custa R&#36; 999.",
    "Relógio Exemplo Clássico 38mm https://www.newstorerj.com/exemplo. Vou encaminhar para um atendente.",
])
async def test_ready_generated_copy_cannot_erase_listing_or_invent_price(ready, generation, bad_copy):
    from app.sales.ready_delivery import try_ready_delivery
    generation.return_value.text = bad_copy
    result = await try_ready_delivery(incoming("Pronta entrega para casamento?"),
        interpretation=interpretation(preferences={"occasion": "casamento"}), recent_turns=[])
    assert "Relógio Exemplo Clássico 38mm" in result.reply_text
    assert "R$ 999" not in result.reply_text and ".com.br/outro" not in result.reply_text
    assert not result.response_metadata.get("used_openai_responder")


@pytest.mark.asyncio
async def test_catalog_model_cannot_replace_actual_options_with_empty_promise(generation):
    generation.return_value.text = "Encontrei boas opções para você. Quer que eu mostre?"
    result = await sales_response_with_openai(IncomingMessage(text="Para um casamento"),
        {"intent": "product_search", "goal": "find"},
        AgentResult(reply_text="Relógio Exemplo Clássico 38mm", commercial_data={"products": [PRODUCT]}),
        interpretation(preferences={"occasion": "casamento"}), recent_turns=[])
    assert result.reply_text == "Relógio Exemplo Clássico 38mm"
    assert result.response_metadata['fallback_reason'] == 'catalog_missing_candidate_identity'


@pytest.mark.asyncio
@pytest.mark.parametrize('preferences', [
    {'budget_max': 3000}, {'mechanism': 'automatic'}, {'style': 'social'},
    {'crystal': 'sapphire'}, {'attributes': ['required_strap_material:leather']},
])
async def test_public_list_cannot_silently_discard_criteria_requiring_product_facts(ready, preferences):
    from app.sales.ready_delivery import try_ready_delivery
    assert await try_ready_delivery(incoming('Pronta entrega para casamento?'),
        interpretation=interpretation(preferences={'occasion': 'casamento', **preferences})) is None
    ready.assert_not_awaited()  # Return to normal, authoritative product retrieval.


@pytest.mark.asyncio
async def test_ready_followup_keeps_scoped_model_when_new_turn_only_adds_occasion(ready):
    from app.sales.ready_delivery import try_ready_delivery
    from app.commerce.commerce_context import evolve_commerce_state
    first = await try_ready_delivery(incoming('Tissot PRX azul 35mm pronta entrega'))
    state = evolve_commerce_state(CommerceConversationState(), first)
    ready.reset_mock()
    await try_ready_delivery(incoming('É para casamento'), state,
        interpretation=interpretation(preferences={'occasion': 'casamento'}, references_previous_context=True))
    ready.assert_awaited_once_with('Tissot PRX azul 35mm pronta entrega')


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [{'goal': 'buy'}, {'payment_action': 'installment'},
                                    {'shipping_action': 'quote'}, {'goal': 'compare'}])
async def test_ready_shortcut_preserves_transaction_and_comparison_routes(ready, changes):
    from app.sales.ready_delivery import try_ready_delivery
    assert await try_ready_delivery(incoming('Esse pronta entrega, vamos fechar?'),
        interpretation=interpretation(**changes)) is None
    ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_door_passes_current_interpretation_and_keeps_generation_provenance(ready, generation, monkeypatch):
    from app.agents import door
    current = interpretation(preferences={'occasion': 'casamento', 'delivery_deadline_text': 'semana que vem'})
    monkeypatch.setattr(door, 'interpret_message', AsyncMock(return_value=current))
    generation.return_value.text = ('Para o casamento: Relógio Exemplo Clássico 38mm\n'
                                   'https://www.newstorerj.com/exemplo')
    history = [{'role': 'user', 'content': 'Quero um relógio'}]
    result = await door._route_after_interpret(message=incoming('Pronta entrega para casamento semana que vem?'),
        customer_context={}, commerce_state=CommerceConversationState(), recovery_turns=history,
        recent_turns=history, model_turns=history, answering_qualification=False, phrase_restart=False)
    ready.assert_awaited_once_with('pronta entrega')
    assert result.response_metadata['used_openai_responder'] is True
    assert result.response_metadata['active_preferences']['occasion'] == 'casamento'
    assert {'role': 'user', 'content': 'Quero um relógio'} in generation.call_args.kwargs['messages']
    from app.ops.handoff_service import enrich_handoff_metadata
    from app.verify.final_response import finalize_response
    message = incoming('Pronta entrega para casamento semana que vem?')
    result = enrich_handoff_metadata(message, result, recent_turns=history)
    result, state = finalize_response(result, incoming=message, interpretation=current,
                                     previous_state=CommerceConversationState())
    assert not result.handoff_required and result.intent != 'handoff'
    assert 'https://www.newstorerj.com/exemplo' in result.reply_text
    assert state.active_preferences['occasion'] == 'casamento'
    assert state.ready_delivery_context and state.active_product is None
