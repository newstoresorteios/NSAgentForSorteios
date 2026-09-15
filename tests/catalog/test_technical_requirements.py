import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.configuration.runtime import current_bundle, bind_bundle, reset_bundle
from app.models import SalesInterpretation
from app.llm.turn_understanding import TurnUnderstanding, turn_understanding_to_sales, sales_to_turn_understanding
from app.catalog.specs.requirements import normalize_requirements, technical_requirements, feature_evidence
from app.catalog.retrieval.hard_filter import hard_filter_products
from app.catalog.retrieval.technical import retrieve_technical_products

ASK = "tem relogio automatico e com cristal de safira até 2500 reais?"


def interpretation():
    return turn_understanding_to_sales(TurnUnderstanding(primary_intent="commerce_find", confidence=.96,
        hard_constraints={"category":"relógio", "mechanism":"automatico", "budget_max":2500},
        soft_preferences={"attributes":["cristal de safira"]}))


def test_structured_mechanism_and_crystal_survive_public_roundtrip():
    interp = interpretation()
    assert normalize_requirements(interp, ASK) == {"mechanism":"automatic", "crystal":"sapphire"}
    restored = SalesInterpretation.model_validate_json(interp.model_dump_json())
    assert technical_requirements(restored) == technical_requirements(interp)
    turn = sales_to_turn_understanding(restored)
    assert turn.hard_constraints.mechanism == "automatic"
    assert turn.hard_constraints.crystal == "sapphire"


@pytest.mark.parametrize("description,status", [
    ("Movimento quartzo. Cristal mineral.", "mismatch"),
    ("Movimento Eco-Drive solar. Cristal mineral.", "mismatch"),
    ("Movimento automático. Cristal de safira.", "matched"),
    ("Movimento automático. Cristal não informado.", "unknown"),
    ("Relógio de aço inoxidável.", "unknown"),
    ("Movimento automático. Não tem safira.", "mismatch"),
])
def test_all_required_features_need_positive_evidence(description, status):
    interp = interpretation()
    required = normalize_requirements(interp, ASK)
    product = {"id":"1", "description":description, "price":2000, "stock":1, "available":True}
    assert feature_evidence(product, required)["status"] == status
    assert bool(hard_filter_products([product], interp, mode="recommendation")) is (status == "matched")


def test_followup_preserves_then_explicitly_changes_and_relaxes_requirements():
    interp = interpretation()
    normalize_requirements(interp, ASK)
    assert normalize_requirements(interp, "e qual o prazo?")["crystal"] == "sapphire"
    assert normalize_requirements(interp, "pode ser cristal mineral")["crystal"] == "mineral"
    assert technical_requirements(interp)["crystal"] == "mineral"
    assert "crystal" not in normalize_requirements(interp, "não precisa de mineral")


def test_operator_can_add_alias_without_code_change():
    bundle = current_bundle()
    rules = json.loads(bundle["values"]["catalogTechnicalFeatures"])
    next(r for r in rules if r["value"] == "sapphire")["aliases"].append("vidro premium confirmado")
    tokens = bind_bundle({**bundle, "values":{**bundle["values"], "catalogTechnicalFeatures":json.dumps(rules)}}, None)
    try:
        assert feature_evidence({"description":"vidro premium confirmado"}, {"crystal":"sapphire"})["status"] == "matched"
    finally:
        reset_bundle(tokens)


@pytest.mark.asyncio
async def test_live_details_exclude_quartz_and_unknown_and_only_show_confirmed(monkeypatch):
    interp = interpretation()
    normalize_requirements(interp, ASK)
    rows = [{"id":str(i), "name":f"Modelo {i}", "price":2200, "stock":2, "available":True} for i in range(1,4)]
    descriptions = {"1":"Quartzo e cristal mineral", "2":"Automático e cristal de safira", "3":"Ficha sem detalhes"}
    calls = []
    async def execute(name, args):
        calls.append(name)
        assert name == "get_product"
        row = next(p for p in rows if p["id"] == args["product_id"])
        return {**row, "description":descriptions[row["id"]]}
    monkeypatch.setattr("app.catalog.index.catalog_index.index_products_best_effort", lambda *a, **k: None)
    session = SimpleNamespace(interpretation=interp, candidates=rows, excluded_ids=set(),
        retrieval_plan=SimpleNamespace(candidate_limit=20, mode="recommendation"),
        list_query_extras=lambda i: {}, search_products=AsyncMock(return_value={"products":[]}), execute_tool=execute)
    result = await retrieve_technical_products(session)
    assert [p["id"] for p in result.commercial_data["products"]] == ["2"]
    assert "Modelo 2" in result.reply_text
    assert "Modelo 1" not in result.reply_text
    assert result.handoff_required is False
    assert calls == ["get_product"] * 3


@pytest.mark.asyncio
async def test_detail_failure_does_not_claim_catalog_absence(monkeypatch):
    interp = interpretation(); normalize_requirements(interp, ASK)
    session = SimpleNamespace(interpretation=interp, candidates=[{"id":"1","price":1000}], excluded_ids=set(),
        retrieval_plan=SimpleNamespace(candidate_limit=20, mode="recommendation"), list_query_extras=lambda i: {},
        search_products=AsyncMock(return_value={"products":[]}), execute_tool=AsyncMock(return_value={"error":"timeout","status_code":504}))
    result = await retrieve_technical_products(session)
    assert result.safety_reason == "catalog_requirements_unknown"
    assert result.commercial_data["products"] == []
    assert not result.handoff_required


def test_legacy_attributes_do_not_discard_unknown_sheet_before_confirmation():
    interp = interpretation()
    interp.preferences.attributes = ["automatico", "required_feature:automatico", "required_feature:safira"]
    normalize_requirements(interp, ASK)
    product = {"id":"1", "name":"Ficha incompleta", "price":2000, "available":True}
    assert hard_filter_products([product],interp,mode="recommendation",allow_unknown_features=True) == [product]
    assert not hard_filter_products([product],interp,mode="recommendation")
    product.update(mechanism="automatic",crystal="sapphire")
    assert hard_filter_products([product],interp,mode="recommendation") == [product]


@pytest.mark.asyncio
async def test_detail_fanout_respects_published_limits():
    interp = interpretation(); normalize_requirements(interp,ASK)
    rows = [{"id":str(i),"name":f"Ficha {i}","price":2000,"available":True} for i in range(30)]
    execute = AsyncMock(side_effect=lambda name,args:next(p for p in rows if p["id"]==args["product_id"]))
    search = AsyncMock(return_value={"products":rows})
    session = SimpleNamespace(interpretation=interp,candidates=rows,excluded_ids=set(),
        retrieval_plan=SimpleNamespace(mode="recommendation",candidate_limit=30),
        list_query_extras=lambda _: {},search_products=search,execute_tool=execute)
    bundle = current_bundle()
    token = bind_bundle({**bundle,"values":{**bundle["values"],"catalogTechnicalDetailLimit":3,"catalogTechnicalSearchLimit":1}},None)
    try:
        result = await retrieve_technical_products(session)
        assert execute.await_count == 3
        assert search.await_count == 1
        assert result.safety_reason == "catalog_requirements_unknown"
    finally:
        reset_bundle(token)
