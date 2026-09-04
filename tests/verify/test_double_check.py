import pytest

from app.commerce.commerce_context import (
    CommerceConversationState,
    CommerceProductReference,
)
from app.identity.greeting_policy import GREETING_REPLY
from app.models import AgentResult, IncomingMessage
from app.verify.double_check import (
    DoubleCheckReport,
    DoubleCheckVerdict,
    apply_double_check,
    apply_double_check_async,
    run_phase0_double_check,
    should_run_phase1_double_check,
)


def test_phase0_skips_greeting_and_raffle():
    greeting = AgentResult(
        reply_text="Olá!",
        intent="general",
        response_metadata={"response_source": "local_greeting", "domain": "greeting"},
    )
    result, report = apply_double_check(
        incoming=IncomingMessage(text="oi"),
        result=greeting,
        mode="shadow",
    )
    assert result.reply_text == "Olá!"
    assert report.skipped is True
    assert report.skip_reason and report.skip_reason.startswith("deterministic:")

    raffle = AgentResult(
        reply_text="O sorteio está aberto.",
        intent="raffle",
        response_metadata={"response_source": "local_raffle", "domain": "raffle"},
    )
    _, raffle_report = apply_double_check(
        incoming=IncomingMessage(text="como funciona o sorteio?"),
        result=raffle,
        mode="shadow",
    )
    assert raffle_report.skipped is True


def test_phase0_pix_denied_is_shadow_only():
    state = CommerceConversationState(
        order_id="25400",
        order_payment_url="https://pay.example/pix",
        pending_action="awaiting_payment",
    )
    original = AgentResult(
        reply_text="Não tenho o link de pagamento agora.",
        intent="commerce",
        response_metadata={"domain": "commerce", "response_source": "openai"},
    )
    result, report = apply_double_check(
        incoming=IncomingMessage(text="me manda o pix"),
        result=original,
        commerce_state=state,
        mode="shadow",
    )
    assert report.approved is False
    assert any(issue.code == "pix_denied" for issue in report.issues)
    assert report.applied is False
    assert result.reply_text == original.reply_text
    assert "https://pay.example/pix" not in result.reply_text
    meta = result.response_metadata["double_check"]
    assert meta["approved"] is False


def test_phase0_pix_denied_enforce_resumes_link():
    state = CommerceConversationState(
        order_id="25400",
        order_payment_url="https://pay.example/pix",
        pending_action="awaiting_payment",
    )
    original = AgentResult(
        reply_text="Não tenho o link de pagamento agora.",
        intent="commerce",
        response_metadata={"domain": "commerce", "response_source": "openai"},
    )
    result, report = apply_double_check(
        incoming=IncomingMessage(text="me manda o pix"),
        result=original,
        commerce_state=state,
        mode="enforce",
    )
    assert report.applied is True
    assert report.applied_code == "pix_denied"
    assert "https://pay.example/pix" in result.reply_text


def test_phase0_budget_over_from_message_not_interpretation():
    listed = AgentResult(
        reply_text="Encontrei o Tissot PRX por R$ 8.000.",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "9", "name": "Tissot PRX", "current_price": 8000},
            ]
        },
        response_metadata={"domain": "commerce", "response_source": "openai"},
    )
    issues = run_phase0_double_check(
        incoming=IncomingMessage(text="quero um até 3000"),
        result=listed,
    )
    assert any(issue.code == "budget_over" for issue in issues)


def test_phase0_color_mismatch_ignores_interpretation():
    listed = AgentResult(
        reply_text="1. Tissot PRX preto",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "2", "name": "Tissot PRX preto", "current_price": 2800},
            ]
        },
        response_metadata={"domain": "commerce", "response_source": "openai"},
    )
    issues = run_phase0_double_check(
        incoming=IncomingMessage(text="quero um azul"),
        result=listed,
    )
    assert any(issue.code == "color_mismatch" for issue in issues)


def test_phase0_council_blocked_still_listing():
    listed = AgentResult(
        reply_text="1. Seiko 5",
        intent="commerce",
        safety_reason="answer_council_blocked",
        commercial_data={"products": [{"id": "1", "name": "Seiko 5"}]},
        response_metadata={"domain": "commerce"},
    )
    issues = run_phase0_double_check(
        incoming=IncomingMessage(text="tem seiko?"),
        result=listed,
    )
    assert any(issue.code == "council_blocked_listing" for issue in issues)


def test_phase0_greeting_in_checkout():
    state = CommerceConversationState(
        order_id="10",
        order_payment_url="https://pay.example/x",
        pending_action="awaiting_payment",
    )
    issues = run_phase0_double_check(
        incoming=IncomingMessage(text="e o pagamento?"),
        result=AgentResult(
            reply_text=GREETING_REPLY,
            intent="commerce",
            response_metadata={"domain": "commerce", "response_source": "openai"},
        ),
        commerce_state=state,
    )
    assert any(issue.code == "greeting_in_checkout" for issue in issues)


def test_phase0_sku_lock_mismatch():
    state = CommerceConversationState(
        pending_action="create_cart",
        active_product=CommerceProductReference(
            product_id="100",
            name="Seiko 5",
        ),
    )
    issues = run_phase0_double_check(
        incoming=IncomingMessage(text="pode fechar"),
        result=AgentResult(
            reply_text="1. Outro modelo",
            intent="commerce",
            commercial_data={
                "products": [{"id": "200", "name": "Outro modelo"}],
            },
            response_metadata={"domain": "commerce", "response_source": "openai"},
        ),
        commerce_state=state,
    )
    assert any(issue.code == "sku_lock_mismatch" for issue in issues)


def test_phase0_mode_off_skips():
    result, report = apply_double_check(
        incoming=IncomingMessage(text="tem seiko?"),
        result=AgentResult(
            reply_text="Seiko 5",
            intent="commerce",
            commercial_data={"products": [{"id": "1", "name": "Seiko 5"}]},
        ),
        mode="off",
    )
    assert report.skipped is True
    assert report.skip_reason == "configured_off"
    assert "double_check" not in (result.response_metadata or {})


def _priced_listing() -> AgentResult:
    return AgentResult(
        reply_text="1. Seiko 5 — R$ 2.500",
        intent="commerce",
        commercial_data={
            "products": [{"id": "1", "name": "Seiko 5", "current_price": 2500}],
        },
        response_metadata={"domain": "commerce", "response_source": "openai"},
    )


def test_phase1_skips_priced_listing_without_high_risk():
    result = _priced_listing()
    _, report = apply_double_check(
        incoming=IncomingMessage(text="tem seiko?"),
        result=result,
        mode="shadow",
    )
    run, gate, signals = should_run_phase1_double_check(
        report=report,
        incoming=IncomingMessage(text="tem seiko?"),
        result=result,
        openai_api_key="sk-test",
    )
    assert run is False
    assert gate == "no_high_risk_signal"
    assert signals == []


def test_phase1_skips_when_phase0_already_vetoed():
    state = CommerceConversationState(
        order_id="10",
        order_payment_url="https://pay.example/x",
        pending_action="awaiting_payment",
    )
    original = AgentResult(
        reply_text="Não tenho o link de pagamento agora.",
        intent="commerce",
        response_metadata={"domain": "commerce", "response_source": "openai"},
    )
    _, report = apply_double_check(
        incoming=IncomingMessage(text="me manda o pix"),
        result=original,
        commerce_state=state,
        mode="shadow",
    )
    run, gate, _signals = should_run_phase1_double_check(
        report=report,
        incoming=IncomingMessage(text="me manda o pix"),
        result=original,
        commerce_state=state,
        openai_api_key="sk-test",
    )
    assert run is False
    assert gate == "phase0_already_vetoed"


def test_phase1_skips_after_critique_or_budget():
    report = DoubleCheckReport(mode="shadow", approved=True)
    incoming = IncomingMessage(text="me manda o pix")
    result = AgentResult(
        reply_text="Seu pedido segue em aberto.",
        intent="commerce",
        response_metadata={"domain": "commerce", "response_source": "openai"},
    )
    state = CommerceConversationState(
        order_payment_url="https://pay.example/x",
        pending_action="awaiting_payment",
    )
    run, gate, _ = should_run_phase1_double_check(
        report=report,
        incoming=incoming,
        result=result,
        commerce_state=state,
        critique_regenerated=True,
        openai_api_key="sk-test",
    )
    assert run is False
    assert gate == "skipped_after_critique_regenerate"
    run, gate, _ = should_run_phase1_double_check(
        report=report,
        incoming=incoming,
        result=result,
        commerce_state=state,
        openai_call_count=3,
        openai_api_key="sk-test",
    )
    assert run is False
    assert gate == "llm_budget"


@pytest.mark.asyncio
async def test_phase1_shadow_veto_does_not_rewrite(monkeypatch):
    incoming = IncomingMessage(text="me manda o pix")
    result = AgentResult(
        reply_text="Seu pedido segue em aberto.",
        intent="commerce",
        response_metadata={"domain": "commerce", "response_source": "openai"},
    )
    state = CommerceConversationState(
        order_id="10",
        order_payment_url="https://pay.example/pix",
        pending_action="awaiting_payment",
    )
    seen: dict[str, object] = {}

    class FakeParse:
        parsed = DoubleCheckVerdict(
            action="veto",
            code="pix",
            reason="url_present_not_in_reply",
        )

    async def fake_parse(**kwargs):
        seen["model"] = kwargs.get("model")
        seen["call_type"] = kwargs.get("call_type")
        return FakeParse()

    monkeypatch.setattr(
        "app.llm.openai_gateway.parse_structured_output",
        fake_parse,
    )
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: type(
            "S",
            (),
            {
                "openai_api_key": "sk-test",
                "openai_model": "gpt-4.1-mini",
                "openai_main_model": "gpt-4.1-mini",
                "openai_fast_model": "gpt-4.1-nano",
            },
        )(),
    )
    monkeypatch.setattr(
        "app.llm.openai_models.resolve_openai_model",
        lambda role="main": "gpt-4.1-nano" if role == "fast" else "gpt-4.1-mini",
    )
    updated, report = await apply_double_check_async(
        incoming=incoming,
        result=result,
        commerce_state=state,
        mode="shadow",
        openai_call_count=1,
    )
    assert report.phase1_ran is True
    assert report.approved is False
    assert report.applied is False
    assert updated.reply_text == "Seu pedido segue em aberto."
    assert seen["call_type"] == "double_check"
    assert seen["model"] == "gpt-4.1-nano"
    assert "https://pay.example/pix" not in updated.reply_text


@pytest.mark.asyncio
async def test_phase1_does_not_call_llm_on_greeting(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("phase1 LLM must not run for greetings")

    monkeypatch.setattr("app.llm.openai_gateway.parse_structured_output", boom)
    result, report = await apply_double_check_async(
        incoming=IncomingMessage(text="oi"),
        result=AgentResult(
            reply_text="Olá!",
            intent="general",
            response_metadata={
                "response_source": "local_greeting",
                "domain": "greeting",
            },
        ),
        mode="shadow",
    )
    assert report.skipped is True
    assert report.phase1_ran is False
    assert result.reply_text == "Olá!"


def test_phase0_greeting_in_checkout_enforce_resumes_pix():
    state = CommerceConversationState(
        order_id="10",
        order_payment_url="https://pay.example/x",
        pending_action="awaiting_payment",
    )
    result, report = apply_double_check(
        incoming=IncomingMessage(text="e o pagamento?"),
        result=AgentResult(
            reply_text=GREETING_REPLY,
            intent="commerce",
            response_metadata={"domain": "commerce", "response_source": "openai"},
        ),
        commerce_state=state,
        mode="enforce",
    )
    assert report.applied is True
    assert report.applied_code == "greeting_in_checkout"
    assert "https://pay.example/x" in result.reply_text


def test_phase0_budget_over_enforce_does_not_rewrite():
    listed = AgentResult(
        reply_text="Encontrei o Tissot PRX por R$ 8.000.",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "9", "name": "Tissot PRX", "current_price": 8000},
            ]
        },
        response_metadata={"domain": "commerce", "response_source": "openai"},
    )
    result, report = apply_double_check(
        incoming=IncomingMessage(text="quero um até 3000"),
        result=listed,
        mode="enforce",
    )
    assert report.approved is False
    assert any(issue.code == "budget_over" for issue in report.issues)
    assert report.applied is False
    assert result.reply_text == listed.reply_text


@pytest.mark.asyncio
async def test_phase1_enforce_pix_resumes_link(monkeypatch):
    incoming = IncomingMessage(text="me manda o pix")
    original = AgentResult(
        reply_text="Seu pedido segue em aberto.",
        intent="commerce",
        response_metadata={"domain": "commerce", "response_source": "openai"},
    )
    state = CommerceConversationState(
        order_id="10",
        order_payment_url="https://pay.example/pix",
        pending_action="awaiting_payment",
    )

    class FakeParse:
        parsed = DoubleCheckVerdict(
            action="veto",
            code="pix",
            reason="url_present_not_in_reply",
        )

    async def fake_parse(**_kwargs):
        return FakeParse()

    monkeypatch.setattr(
        "app.llm.openai_gateway.parse_structured_output",
        fake_parse,
    )
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: type(
            "S",
            (),
            {
                "openai_api_key": "sk-test",
                "openai_model": "gpt-4.1-mini",
                "openai_main_model": "gpt-4.1-mini",
                "openai_fast_model": "gpt-4.1-nano",
            },
        )(),
    )
    monkeypatch.setattr(
        "app.llm.openai_models.resolve_openai_model",
        lambda role="main": "gpt-4.1-nano" if role == "fast" else "gpt-4.1-mini",
    )
    updated, report = await apply_double_check_async(
        incoming=incoming,
        result=original,
        commerce_state=state,
        mode="enforce",
        openai_call_count=1,
    )
    assert report.phase1_ran is True
    assert report.applied is True
    assert report.applied_code == "pix"
    assert "https://pay.example/pix" in updated.reply_text
