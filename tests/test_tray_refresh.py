from types import SimpleNamespace

import pytest

from app.commerce.commerce_context import CommerceConversationState, PresentedCommerceProduct
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.sales.dialogue_phase import blocks_greeting_fast_path, is_open_sale_state
from app.sales.tray_refresh import (
    constraint_requires_tray_refresh,
    excluded_product_ids_for_turn,
    should_drop_contextual_resolve,
    tray_list_query_extras,
)


def _interp(**kwargs) -> SalesInterpretation:
    subject = {
        "product_type": kwargs.pop("product_type", "relógio"),
        "brand": kwargs.pop("brand", "Baltic"),
        "model": kwargs.pop("model", None),
    }
    return SalesInterpretation(
        domain="commerce",
        goal=kwargs.pop("goal", "recommend"),
        subject=subject,
        preferences=kwargs.pop("preferences", {}),
        information_needed=["catalog"],
        references_previous_context=True,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.95,
    )


def test_color_this_turn_requires_tray():
    assert constraint_requires_tray_refresh(
        _interp(model="Aquascaphe mk2"),
        "quero o mk2 cinza 37mm",
    )


def test_generic_recommendation_without_this_turn_constraint_may_skip_tray():
    assert constraint_requires_tray_refresh(
        _interp(brand="Seiko", model=None, preferences={"budget_max": 5000}),
        None,
    ) is False


def test_sku_lock_requires_tray():
    assert constraint_requires_tray_refresh(
        _interp(model="Aquascaphe mk2"),
        "ainda quero esse",
    )


def test_drop_contextual_resolve_on_color_refinement():
    assert should_drop_contextual_resolve(
        interpretation=_interp(model="Aquascaphe mk2"),
        message_text="quero o cinza 37mm",
        purchase_close=False,
    )


def test_keep_contextual_resolve_on_purchase_close():
    assert should_drop_contextual_resolve(
        interpretation=_interp(model="Aquascaphe mk2"),
        message_text="quero o 2",
        purchase_close=True,
    ) is False


def test_keep_contextual_resolve_on_current_product_color_variant():
    interp = _interp(model="Aquascaphe mk2")
    interp = interp.model_copy(update={"reference_type": "current_product"})
    assert should_drop_contextual_resolve(
        interpretation=interp,
        message_text="tem ele preto?",
        purchase_close=False,
    ) is False


def test_excluded_ids_drop_hermetique_when_mk2_locked():
    state = CommerceConversationState(
        last_presented_products=[
            PresentedCommerceProduct(
                product_id="h1",
                name="Baltic Hermétique Summer Cinza 37mm",
                brand="Baltic",
                position=1,
            )
        ]
    )
    ids = excluded_product_ids_for_turn(
        _interp(model="Aquascaphe mk2"),
        "quero o mk2 cinza 37mm",
        state,
    )
    assert "h1" in ids


def test_tray_extras_send_current_price_range_and_color():
    extras = tray_list_query_extras(
        _interp(preferences={"budget_max": 10000, "color": "cinza"})
    )
    assert extras["current_price_range"] == "0,10000"
    assert extras["property_name"] == "Cor"
    assert extras["property_value"] == "Cinza"


def test_open_sale_and_greeting_block():
    shortlist = CommerceConversationState(
        dialogue_phase="shortlist",
        last_presented_products=[
            PresentedCommerceProduct(product_id="1", name="MK2", position=1)
        ],
        active_preferences={"locked_identity": {"model": "Aquascaphe mk2"}},
    )
    checkout = CommerceConversationState(
        dialogue_phase="checkout",
        cart_session_id="s1",
        last_presented_products=shortlist.last_presented_products,
    )
    assert is_open_sale_state(shortlist) is True
    assert blocks_greeting_fast_path(shortlist) is True
    assert blocks_greeting_fast_path(checkout) is False


@pytest.mark.asyncio
async def test_mk2_color_refinement_does_not_skip_tray(monkeypatch):
    import app.sales_agent as sales_agent

    hermetique = {
        "id": "h1",
        "product_id": "h1",
        "name": "Relógio Baltic Hermétique Summer Automático Cinza 37mm",
        "brand": "Baltic",
        "price": 8900,
        "available": True,
        "available_in_store": True,
    }
    mk2 = {
        "id": "m1",
        "product_id": "m1",
        "name": "Relógio Baltic Aquascaphe MK2 Automático Cinza 37mm",
        "brand": "Baltic",
        "price": 9200,
        "available": True,
        "available_in_store": True,
    }
    calls: list[tuple] = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        if name == "search_products":
            return {"products": [mk2]}
        if name in {"list_categories", "get_category_tree", "get_category"}:
            return {"categories": []}
        if name == "get_product":
            return mk2 if str(arguments.get("product_id")) == "m1" else hermetique
        if name == "list_product_variants":
            return {"variants": []}
        return {}

    index_products = [hermetique] + [
        {
            "id": str(i),
            "product_id": str(i),
            "name": f"Baltic Hermétique {i}",
            "brand": "Baltic",
            "price": 8000 + i,
            "available": True,
            "available_in_store": True,
        }
        for i in range(2, 10)
    ]

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    monkeypatch.setattr(
        "app.catalog.catalog_index_primary.fetch_primary_index_candidates",
        lambda *a, **k: (index_products, "constraints"),
    )
    monkeypatch.setattr(
        "app.catalog.catalog_index.index_products_best_effort",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        "app.catalog.product_retrieval.get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(
            agent_catalog_index_read_enabled=True,
            agent_catalog_index_write_enabled=False,
            agent_catalog_index_fallback_to_tray=True,
            agent_catalog_index_candidate_limit=30,
            agent_persona_tenant_id="newstore",
            openai_api_key="",
            openai_model="gpt-4.1-mini",
        ),
    )

    interpretation = _interp(
        model="Aquascaphe mk2",
        preferences={"budget_max": 15000, "color": "cinza"},
    )
    result = await sales_agent._execute_compiled_product_retrieval(
        interpretation,
        message_text="quero o mk2 cinza 37mm",
        commerce_state=CommerceConversationState(
            last_presented_products=[
                PresentedCommerceProduct(
                    product_id="h1",
                    name="Relógio Baltic Hermétique Summer Automático Cinza 37mm",
                    brand="Baltic",
                    position=1,
                )
            ],
            active_preferences={
                "locked_identity": {"brand": "Baltic", "model": "Aquascaphe mk2"}
            },
        ),
    )
    search_calls = [c for c in calls if c[0] == "search_products"]
    assert search_calls, "color/model change must consult Tray, not skip the index"
    assert result is not None
    names = [
        str(item.get("name") or "").casefold()
        for item in (result.commercial_data or {}).get("products") or []
    ]
    assert names
    assert all("herm" not in name for name in names)
    assert any("mk2" in name for name in names)


@pytest.mark.asyncio
async def test_greeting_fast_path_skipped_on_live_shortlist(monkeypatch):
    import app.sales_agent as sales_agent

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    interpreted = await sales_agent.interpret_message(
        IncomingMessage(text="tudo bem?"),
        commerce_state=CommerceConversationState(
            dialogue_phase="shortlist",
            last_presented_products=[
                PresentedCommerceProduct(
                    product_id="m1",
                    name="Baltic Aquascaphe MK2",
                    position=1,
                )
            ],
            active_preferences={
                "locked_identity": {"brand": "Baltic", "model": "Aquascaphe mk2"}
            },
        ),
    )
    assert interpreted._fallback_reason != "greeting_fast_path"
