from __future__ import annotations

from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.outbound_compliance import apply_outbound_compliance, check_outbound_compliance
from app.preference_normalize import (
    normalize_sales_interpretation,
    repair_dial_and_case_preferences,
)
from app.product_media import storefront_url_candidates
from app.product_retrieval import prefer_dial_and_case_matches, score_catalog_candidates
from app.models import ProductPreferences


def test_repair_dourado_visor_preto_splits_dial_and_case():
    prefs = ProductPreferences(color="dourado")
    repaired = repair_dial_and_case_preferences(
        prefs,
        message_text="Eu quero um buloca dourado com o visor preto",
    )
    assert repaired.color == "preto"
    assert repaired.material == "dourado"


def test_normalize_sales_interpretation_repairs_bulova_ask():
    sales = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Bulova", "product_type": "relógio"},
        preferences={"color": "dourado"},
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )
    normalized = normalize_sales_interpretation(
        sales,
        message_text="Eu quero um buloca dourado com o visor preto",
    )
    assert normalized.preferences.color == "preto"
    assert normalized.preferences.material == "dourado"


def test_prefer_dial_and_case_surfaces_preto_com_dourado():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Bulova", "product_type": "relógio"},
        preferences={"color": "preto", "material": "dourado"},
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )
    products = [
        {
            "id": "1",
            "brand": "Bulova",
            "name": "Relogio Bulova Marine Star Serie C automatico preto 96A288",
        },
        {
            "id": "2",
            "brand": "Bulova",
            "name": "Relogio Bulova Marine Star preto com dourado 98B278",
        },
        {
            "id": "3",
            "brand": "Bulova",
            "name": "Relogio Bulova Classic automatico preto 96C131",
        },
    ]
    ranked = prefer_dial_and_case_matches(products, interpretation, limit=3)
    assert ranked
    assert "dourado" in ranked[0]["name"].casefold()
    assert "preto" in ranked[0]["name"].casefold()

    scored = score_catalog_candidates(products, interpretation, limit=3)
    assert scored[0]["id"] == "2"


def test_storefront_candidates_include_bulova_path_repair():
    url = (
        "https://www.newstorerj.com.br/relogios-bulova/"
        "relogio-seminovo-bulova-marine-star-serie-c-automatico-preto-96a288"
    )
    joined = "\n".join(storefront_url_candidates(url))
    assert "/relogios/relogios-bulova/" in joined


def test_compliance_rewrites_dead_photo_link():
    incoming = IncomingMessage(text="me manda as fotos", channel="whatsapp")
    result = AgentResult(
        reply_text=(
            "Não consegui puxar a foto agora, mas aqui está o link oficial:\n"
            "https://www.newstorerj.com.br/relogios-bulova/dead"
        ),
        intent="commerce",
        handoff_required=False,
        safety_reason="product_media_link_fallback",
        commercial_data={
            "products": [
                {
                    "name": "Bulova 96A288",
                    "url": "https://www.newstorerj.com.br/relogios-bulova/dead",
                    "_product_url_dead": True,
                }
            ]
        },
        response_metadata={"domain": "commerce"},
    )
    report = check_outbound_compliance(incoming=incoming, result=result)
    assert report.verdict is not None
    assert report.verdict.pass_check is False
    assert "photo_fallback_dead_link" in report.verdict.issues

    fixed, applied = apply_outbound_compliance(incoming=incoming, result=result)
    assert applied.applied is True
    assert "https://" not in (fixed.reply_text or "")
    assert fixed.safety_reason == "product_media_dead_link"


def test_compliance_flags_gold_ignored_in_shortlist():
    incoming = IncomingMessage(
        text="quero bulova dourado com visor preto",
        channel="whatsapp",
    )
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Bulova", "product_type": "relógio"},
        preferences={"color": "preto", "material": "dourado"},
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )
    result = AgentResult(
        reply_text="Encontrei 2 Bulova com visor preto.",
        intent="commerce",
        handoff_required=False,
        commercial_data={
            "products": [
                {"name": "Bulova Marine Star preto 96A288"},
                {"name": "Bulova Classic preto 96C131"},
            ]
        },
        response_metadata={"domain": "commerce"},
    )
    report = check_outbound_compliance(
        incoming=incoming,
        result=result,
        interpretation=interpretation,
    )
    assert report.verdict is not None
    assert "listed_options_ignore_requested_gold" in report.verdict.issues

    fixed, applied = apply_outbound_compliance(
        incoming=incoming,
        result=result,
        interpretation=interpretation,
    )
    assert applied.applied is True
    assert applied.reresearch_applied is True
    assert "dourado" in (fixed.reply_text or "").casefold()
    assert fixed.safety_reason == "compliance_preference_reresearch"


def test_compliance_reresearch_surfaces_gold_match():
    incoming = IncomingMessage(
        text="quero bulova dourado com visor preto",
        channel="whatsapp",
    )
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Bulova", "product_type": "relógio"},
        preferences={"color": "preto", "material": "dourado"},
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )
    result = AgentResult(
        reply_text=(
            "Encontrei opções com visor preto, mas nenhum veio confirmado "
            "em dourado nessa filtragem."
        ),
        intent="commerce",
        handoff_required=False,
        commercial_data={
            "products": [
                {"id": "1", "name": "Bulova Marine Star preto 96A288"},
                {
                    "id": "2",
                    "name": "Relogio Bulova Marine Star preto com dourado 98B278",
                    "url": "https://www.newstorerj.com.br/relogios/relogios-bulova/x",
                },
                {"id": "3", "name": "Bulova Classic preto 96C131"},
            ]
        },
        response_metadata={"domain": "commerce"},
    )
    report = check_outbound_compliance(
        incoming=incoming,
        result=result,
        interpretation=interpretation,
    )
    assert report.verdict is not None
    assert "false_negative_gold_case" in report.verdict.issues

    fixed, applied = apply_outbound_compliance(
        incoming=incoming,
        result=result,
        interpretation=interpretation,
    )
    assert applied.reresearch_applied is True
    products = (fixed.commercial_data or {}).get("products") or []
    assert products
    assert "dourado" in products[0]["name"].casefold()
    assert "98B278" in (fixed.reply_text or "")
