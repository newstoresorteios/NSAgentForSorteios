"""Tests for gender/budget preference normalization and retrieval mode."""

from app.models import SalesInterpretation
from app.catalog.preference_normalize import (
    detect_gender_label,
    extract_stated_color,
    extract_stated_gender,
    extract_stated_style,
    is_gender_only_label,
    normalize_sales_interpretation,
    preference_gender_label,
    recent_user_context_text,
)
from app.catalog.product_retrieval import ProductRetrievalCompiler, preference_gender_tokens


def _base(**kwargs) -> SalesInterpretation:
    data = {
        "domain": "commerce",
        "goal": "discover",
        "subject": {"product_type": "relógio"},
        "preferences": {},
        "information_needed": ["catalog"],
        "references_previous_context": True,
        "enough_information_to_search": False,
        "ready_for_retrieval": False,
        "stop_clarification": False,
        "needs_clarification": True,
        "clarification_question": "Qual estilo?",
        "confidence": 0.9,
    }
    data.update(kwargs)
    return SalesInterpretation.model_validate(data)


def test_detect_gender_from_feminino_ate_3000():
    assert detect_gender_label("feminino até 3000 reais") == "feminino"


def test_normalize_moves_gender_off_model_and_enables_recommendation():
    interpretation = _base(
        subject={"product_type": "relógio", "model": "feminino"},
        preferences={"style": "feminino", "budget_max": 3000},
    )
    normalized = normalize_sales_interpretation(
        interpretation,
        message_text="feminino até 3000 reais",
    )
    assert normalized.subject.model is None
    assert preference_gender_label(normalized) == "feminino"
    assert normalized.preferences.budget_max == 3000
    assert normalized.ready_for_retrieval is True
    assert normalized.needs_clarification is False
    assert normalized.goal == "recommend"

    plan = ProductRetrievalCompiler.compile(normalized)
    assert plan.mode == "recommendation"
    names = [req.name for req in plan.requests if req.name]
    assert any("feminino" in (name or "").lower() for name in names)
    assert preference_gender_tokens(normalized)[0] == "feminino"


def test_gender_only_label_never_exact():
    assert is_gender_only_label("feminino") is True
    assert is_gender_only_label("Seastar") is False

    interpretation = _base(
        subject={"product_type": "relógio", "model": "feminino"},
        preferences={"budget_max": 3000},
        needs_clarification=False,
    )
    normalized = normalize_sales_interpretation(
        interpretation,
        message_text="feminino até 3000",
    )
    plan = ProductRetrievalCompiler.compile(normalized)
    assert plan.mode == "recommendation"


def test_normalize_prx_integrated_bracelet_and_brushed_case_from_context():
    interpretation = _base(
        subject={"product_type": "relógio", "brand": "Tissot"},
        preferences={},
        needs_clarification=False,
        enough_information_to_search=True,
        ready_for_retrieval=True,
    )
    context = (
        "Quero o Tissot PRX com pulseira integrada\n"
        "A caixa é rajada, escovada"
    )
    normalized = normalize_sales_interpretation(
        interpretation,
        message_text="tem disponível?",
        context_text=context,
    )
    attrs = {item.casefold() for item in normalized.preferences.attributes}
    assert "pulseira_integrada" in attrs
    assert "acabamento_escovado" in attrs
    assert normalized.subject.model == "PRX"
    assert normalized.preferences.material == "prata"


def test_recent_user_context_text_collects_user_turns():
    turns = [
        {"role": "assistant", "content": "Olá"},
        {"role": "user", "content": "PRX integrado"},
        {"role": "user", "content": "caixa escovada"},
    ]
    assert "PRX integrado" in recent_user_context_text(turns)
    assert "caixa escovada" in recent_user_context_text(turns)


def test_extract_stated_color_style_gender_from_message():
    assert extract_stated_color("quero um seiko azul") == "azul"
    assert extract_stated_color("quero um seiko navy") == "azul"
    assert extract_stated_style("quero um relogio esportivo") == "esportivo"
    assert extract_stated_gender("quero um relogio feminino") == "feminino"
    assert extract_stated_color("quero um relogio") is None
