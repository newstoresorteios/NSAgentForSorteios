import pytest
from app.catalog.specs.preference_normalize import extract_bare_budget_amount
from app.memory.contact_preference_memory import rehydrate_interpretation_from_memories
from app.memory.memory_models import ContactMemory
from app.models import SalesInterpretation


@pytest.mark.parametrize("text,expected", [("2700",2700), ("2.700,50",2700.5),
    ("2700.50",2700.5), ("R$ 2 500,99",2500.99), ("2",None), ("641",None),
    ("SKU-2700",None)])
def test_bare_budget_preserves_cents_and_does_not_parse_selection(text,expected):
    assert extract_bare_budget_amount(text)==expected


@pytest.mark.parametrize("continuation", [True,False])
def test_budget_continuation_preserves_brand_but_new_search_does_not(continuation):
    memory=ContactMemory(id=1,tenant_id="test",sender_key="test",memory_key="brand_preference",
        memory_kind="brand_preference",value={"active":"Seiko"})
    interp=SalesInterpretation(domain="commerce",goal="recommend",confidence=.99,
        needs_clarification=False,references_previous_context=continuation,
        preferences={"budget_max":2700})
    result,_=rehydrate_interpretation_from_memories(interp,[memory],message_text="2700")
    assert result.subject.brand == ("Seiko" if continuation else None)
