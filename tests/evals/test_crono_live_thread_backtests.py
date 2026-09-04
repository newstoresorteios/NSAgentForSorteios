"""Offline backtests of the live WhatsApp threads that broke talk-first + search.

Each step records expected vs actual so a failure shows the contract, not only
an assertion line. No live OpenAI/Tray. Do not special-case sender phones.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.catalog.product_retrieval import commercial_availability_facts, hard_filter_products
from app.catalog.specs.catalog_specs import extract_case_size_range_from_text
from app.catalog.specs.preference_normalize import normalize_sales_interpretation
from app.commerce.commerce_context import CommerceConversationState
from app.llm.turn_understanding import TurnUnderstanding, attach_turn_understanding
from app.models import SalesInterpretation
from app.persona.persona_runtime import (
    build_persona_runtime,
    reset_persona_runtime,
    set_persona_runtime,
)
from app.sales.answer_council import apply_turn_contract_for_search, build_turn_contract
from app.sales.intent_router import should_skip_catalog_fanout
from app.sales.tray_query_authority import budget_hard_miss_result
from tests.evals.test_sales_golden_backtests import _crono_chatbo_profile, _persona


@dataclass
class Step:
    turn: str
    expected: object
    actual: object

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


def _assert_steps(steps: list[Step]) -> None:
    failed = [step for step in steps if not step.ok]
    if not failed:
        return
    lines = ["expected vs actual:"]
    for step in steps:
        mark = "OK" if step.ok else "FAIL"
        lines.append(f"  [{mark}] {step.turn}: expected={step.expected!r} actual={step.actual!r}")
    raise AssertionError("\n".join(lines))


def _commerce(**kwargs) -> SalesInterpretation:
    data = {
        "domain": "commerce",
        "goal": "recommend",
        "subject": {"product_type": "relógio"},
        "preferences": {},
        "information_needed": ["catalog"],
        "references_previous_context": True,
        "enough_information_to_search": False,
        "ready_for_retrieval": False,
        "needs_clarification": True,
        "confidence": 0.9,
    }
    data.update(kwargs)
    return SalesInterpretation(**data)


@pytest.mark.offline_eval
def test_golden_bulova_thread_asks_budget_then_unlocks_search():
    """DDD 43 live path: Bulova dourado → ask faixa → Até 3500 searches Bulova."""
    import app.sales_agent as sales_agent

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    steps: list[Step] = []
    try:
        ask = normalize_sales_interpretation(
            _commerce(
                subject={"brand": "Bulova", "product_type": "relógio"},
                preferences={"color": "dourado", "attributes": ["automático"]},
            ),
            message_text="Quero um bulova automático dourado",
        )
        discovery = sales_agent._discovery_state(
            ask,
            [],
            message_text="Quero um bulova automático dourado",
        )
        question = sales_agent._persona_qualification_question(ask, discovery)
        steps.extend(
            [
                Step("t1.skip_catalog_fanout", False, should_skip_catalog_fanout(ask)),
                Step(
                    "t1.persona_qualification_required",
                    True,
                    bool(discovery.get("persona_qualification_required")),
                ),
                Step("t1.force_retrieval", False, bool(discovery.get("force_retrieval"))),
                Step(
                    "t1.asks_investimento",
                    True,
                    bool(question and "investimento" in question.casefold()),
                ),
            ]
        )

        with_budget = normalize_sales_interpretation(
            ask.model_copy(deep=True),
            message_text="Até 3500",
            context_text="Quero um bulova automático dourado",
        )
        discovery2 = sales_agent._discovery_state(
            with_budget,
            [],
            message_text="Até 3500",
        )
        steps.extend(
            [
                Step("t2.brand", "Bulova", with_budget.subject.brand),
                Step("t2.color", "dourado", with_budget.preferences.color),
                Step("t2.budget_max", 3500.0, float(with_budget.preferences.budget_max or 0)),
                Step("t2.force_retrieval", True, bool(discovery2.get("force_retrieval"))),
                Step(
                    "t2.skip_catalog_fanout",
                    False,
                    should_skip_catalog_fanout(with_budget),
                ),
            ]
        )
        _assert_steps(steps)
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_golden_any_brand_2500_open_case_size_and_honest_miss():
    """DDD 85 live path: até 2500 + caixa acima de 40mm + qualquer marca."""
    import app.sales_agent as sales_agent

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    steps: list[Step] = []
    try:
        message = "Quero relógios até 2500, caixa acima de 40mm"
        sized = normalize_sales_interpretation(
            _commerce(preferences={"budget_max": 2500}),
            message_text=message,
        )
        discovery = sales_agent._discovery_state(
            sized,
            [],
            message_text=message,
        )
        pool = [
            {
                "id": "small",
                "name": "Seiko 38mm",
                "brand": "Seiko",
                "case_size": "38",
                "current_price": 1800,
                "available": True,
            },
            {
                "id": "large",
                "name": "Tissot 42mm",
                "brand": "Tissot",
                "case_size": "42",
                "current_price": 2100,
                "available": True,
            },
            {
                "id": "over",
                "name": "Omega 42mm",
                "brand": "Omega",
                "case_size": "42",
                "current_price": 8900,
                "available": True,
            },
        ]
        filtered = hard_filter_products(
            pool,
            sized,
            mode="recommendation",
            message_text=message,
        )
        miss = budget_hard_miss_result(
            sized,
            [
                {
                    "id": "over",
                    "name": "Omega 42mm",
                    "brand": "Omega",
                    "case_size": "42",
                    "current_price": 8900,
                    "price": 8900,
                    "available": True,
                }
            ],
        )
        steps.extend(
            [
                Step("case_parse", (40, 55), extract_case_size_range_from_text(message)),
                Step("t1.brand", None, sized.subject.brand),
                Step("t1.budget_max", 2500.0, float(sized.preferences.budget_max or 0)),
                Step("t1.case_size_range", (40, 55), discovery.get("case_size_range")),
                Step("t1.hard_filter_ids", ["large"], [item["id"] for item in filtered]),
                Step("t1.miss_invents_brand", False, "essa marca" in (miss.reply_text or "")),
                Step(
                    "t1.miss_says_relogios",
                    True,
                    "relógios" in (miss.reply_text or "").casefold() if miss else False,
                ),
            ]
        )

        sticky = _commerce(
            subject={"brand": "Tag Heuer", "product_type": "relógio"},
            preferences={"budget_max": 2500},
            enough_information_to_search=True,
            ready_for_retrieval=True,
            needs_clarification=False,
        )
        unlocked = normalize_sales_interpretation(sticky, message_text="Qualquer marca")
        contract = build_turn_contract(
            message_text="Qualquer marca",
            interpretation=unlocked,
            commerce_state=CommerceConversationState(
                active_preferences={
                    "budget": {"max": 2500},
                    "locked_identity": {"brand": "Tag Heuer"},
                }
            ),
        )
        bound = apply_turn_contract_for_search(
            unlocked,
            message_text="Qualquer marca",
            commerce_state=CommerceConversationState(
                active_preferences={
                    "budget": {"max": 2500},
                    "locked_identity": {"brand": "Tag Heuer"},
                }
            ),
        )
        steps.extend(
            [
                Step("t2.normalized_brand", None, unlocked.subject.brand),
                Step("t2.contract_brand", None, contract.brand),
                Step("t2.bound_brand", None, bound.subject.brand),
                Step("t2.brand_lock", False, "brand_lock" in contract.hard_codes),
                Step("t2.ready_for_retrieval", True, bool(unlocked.ready_for_retrieval)),
            ]
        )
        _assert_steps(steps)
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_golden_inspect_battery_skips_catalog_fanout():
    """Inspect 'usa bateria?' must talk from the active SKU, not fan-out Tray."""
    inspect = _commerce(
        goal="inspect",
        subject={"brand": "Bulova", "product_type": "relógio"},
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
    )
    attach_turn_understanding(
        inspect,
        TurnUnderstanding(
            primary_intent="commerce_inspect",
            answer_strategy="answer_directly",
            confidence=0.9,
        ),
    )
    facts = commercial_availability_facts(
        {
            "stock": 2,
            "available": True,
            "order_days_availability": "15 dias úteis",
        }
    )
    _assert_steps(
        [
            Step("skip_catalog_fanout", True, should_skip_catalog_fanout(inspect)),
            Step("string_lead_time_days", 15, facts.get("lead_time_days")),
        ]
    )


@pytest.mark.offline_eval
@pytest.mark.asyncio
async def test_golden_budget_this_turn_uses_index_not_tray_fanout(monkeypatch):
    """'Até 3500' must present from a sufficient index pool, not force Tray lists."""
    import app.sales_agent as sales_agent
    from app.models import ProductPreferences, ProductSubject

    calls: list[tuple] = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        if name == "list_categories":
            return {"categories": []}
        if name == "get_product":
            return {
                "id": arguments["product_id"],
                "name": f"Bulova {arguments['product_id']}",
                "brand": "Bulova",
                "current_price": 2800,
                "available": True,
                "available_in_store": True,
            }
        raise AssertionError(f"unexpected tray call: {name} {arguments}")

    index_products = [
        {
            "id": str(i),
            "product_id": str(i),
            "name": f"Bulova Automático Dourado {i}",
            "brand": "Bulova",
            "price": 2500 + i * 50,
            "current_price": 2500 + i * 50,
            "available": True,
            "available_in_store": True,
            "_from_catalog_index": True,
            "_factual_source": "catalog_index",
        }
        for i in range(1, 9)
    ]
    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    monkeypatch.setattr(
        "app.catalog.index.primary.fetch_primary_index_candidates",
        lambda *a, **k: (index_products, "constraints"),
    )
    monkeypatch.setattr(
        "app.catalog.index.catalog_index.index_products_best_effort",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        "app.catalog.retrieval.runtime.get_settings",
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
    result = await sales_agent._execute_compiled_product_retrieval(
        SalesInterpretation(
            domain="commerce",
            goal="recommend",
            subject=ProductSubject(brand="Bulova", product_type="relógio"),
            preferences=ProductPreferences(budget_max=3500, color="dourado"),
            information_needed=["catalog"],
            references_previous_context=True,
            enough_information_to_search=True,
            ready_for_retrieval=True,
            needs_clarification=False,
            confidence=0.95,
        ),
        message_text="Até 3500",
    )
    search_calls = [c for c in calls if c[0] == "search_products"]
    products = (result.commercial_data or {}).get("products") if result else []
    _assert_steps(
        [
            Step("tray_list_search_count", 0, len(search_calls)),
            Step("presented_from_index", True, bool(products)),
            Step("http_500_silence", False, result is None),
        ]
    )
