import pytest
from app.catalog.specs.preference_normalize import extract_bare_budget_amount
from app.memory.contact_preference_memory import rehydrate_interpretation_from_memories
from app.memory.memory_models import ContactMemory
from app.models import SalesInterpretation


@pytest.mark.parametrize('text,expected', [
    ('Consulte Citizen NY0120-01EE. Qual o material da pulseira?', None),
    ('Promaster 8204, material borracha', None),
    ('NY0120-01EE por até R$ 3.500', 3500),
    ('Calibre 8204, orçamento R$ 3.500', 3500),
    ('Calibre 8204, orçamento 3500 reais', 3500),
    ('Até 3 mil', 3000), ('No máximo 2500', 2500),
    ('Material em aço, 42 mm', None),
])
def test_budget_requires_local_currency_or_ceiling_cue(text, expected):
    from app.catalog.specs.preference_normalize import _extract_budget_max
    assert _extract_budget_max(text) == expected


@pytest.mark.parametrize("text,expected", [("2700",2700), ("2.700,50",2700.5),
    ("2700.50",2700.5), ("R$ 2 500,99",2500.99), ("2",None), ("641",None),
    ("SKU-2700",None)])
def test_bare_budget_preserves_cents_and_does_not_parse_selection(text,expected):
    assert extract_bare_budget_amount(text)==expected


@pytest.mark.parametrize("continuation", [True,False])
def test_budget_continuation_never_imports_brand_from_durable_memory(continuation):
    memory=ContactMemory(id=1,tenant_id="test",sender_key="test",memory_key="brand_preference",
        memory_kind="brand_preference",value={"active":"Seiko"})
    interp=SalesInterpretation(domain="commerce",goal="recommend",confidence=.99,
        needs_clarification=False,references_previous_context=continuation,
        preferences={"budget_max":2700})
    result,_=rehydrate_interpretation_from_memories(interp,[memory],message_text="2700")
    # Current-session brand is recovered from its question/state (covered by
    # adaptive discovery tests), never inferred from a lifetime preference.
    assert result.subject.brand is None
