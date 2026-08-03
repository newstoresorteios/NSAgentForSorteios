import pytest

from app.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage
from app.response_critique import (
    CritiqueVerdict,
    RecommendedApiCall,
    apply_response_critique_loop,
    _fill_api_arguments,
    _seed_args_from_context,
)
from app.capability_catalog import build_capability_catalog, RETRYABLE_API_NAMES


def test_capability_catalog_includes_order_payment_apis():
    catalog = build_capability_catalog()
    assert "get_order_payment" in catalog["commerce_apis"]
    assert "get_order_payment" in RETRYABLE_API_NAMES
    assert catalog["policy"]


def test_fill_api_arguments_uses_order_seed():
    seeds = _seed_args_from_context(
        state=CommerceConversationState(order_id="25400"),
        result=AgentResult(reply_text="x", intent="commerce"),
    )
    args = _fill_api_arguments(
        RecommendedApiCall(name="get_order_payment", arguments={}),
        seeds,
    )
    assert args == {"order_id": "25400"}


@pytest.mark.asyncio
async def test_critique_enforce_retries_api_and_regenerates(monkeypatch):
    incoming = IncomingMessage(
        channel="whatsapp",
        text="me da o link para pagamento",
    )
    result = AgentResult(
        reply_text="Ainda não há pedido criado nem link de pagamento disponível.",
        intent="commerce",
        commercial_data={},
    )
    state = CommerceConversationState(order_id="25400")
    turns = [
        {
            "role": "assistant",
            "content": (
                "Use este link: https://www.newstorerj.com.br/loja/pagamento.php"
                "?loja=687890&pedido=0CC131B51070AEF"
            ),
        }
    ]
    calls = {"judge": 0, "tools": []}

    async def fake_judge(**kwargs):
        calls["judge"] += 1
        if calls["judge"] == 1:
            return CritiqueVerdict(
                score=20,
                pass_check=False,
                issues=["ignored_existing_payment_link"],
                summary="link exists in transcript",
                recommended_apis=[
                    RecommendedApiCall(
                        name="get_order_payment",
                        arguments={"order_id": "25400"},
                        reason="recover payment url",
                    )
                ],
                retry_instruction="Return the existing payment link",
                better_reply_hint="Send the payment URL from facts",
            )
        return CritiqueVerdict(
            score=95,
            pass_check=True,
            issues=[],
            summary="ok",
        )

    async def fake_execute(name, args):
        calls["tools"].append((name, args))
        return {
            "success": True,
            "payment": {
                "payment_url": (
                    "https://www.newstorerj.com.br/loja/pagamento.php"
                    "?loja=687890&pedido=0CC131B51070AEF"
                ),
                "has_payment": False,
            },
        }

    async def fake_regen(**kwargs):
        regenerated = kwargs["result"].model_copy(deep=True)
        regenerated.reply_text = (
            "Segue o link: https://www.newstorerj.com.br/loja/pagamento.php"
            "?loja=687890&pedido=0CC131B51070AEF"
        )
        regenerated.commercial_data = {
            "order_id": "25400",
            "payment": {
                "payment_url": (
                    "https://www.newstorerj.com.br/loja/pagamento.php"
                    "?loja=687890&pedido=0CC131B51070AEF"
                )
            },
        }
        return regenerated

    monkeypatch.setattr(
        "app.response_critique.run_critique_judge",
        fake_judge,
    )
    monkeypatch.setattr(
        "app.response_critique._regenerate_reply",
        fake_regen,
    )

    final, report = await apply_response_critique_loop(
        incoming=incoming,
        result=result,
        recent_turns=turns,
        commerce_state=state,
        mode="enforce",
        max_retries=2,
        execute=fake_execute,
    )

    assert report.regenerated is True
    assert report.approved is True
    assert "pagamento.php" in final.reply_text
    assert calls["tools"][0][0] == "get_order_payment"
    assert final.response_metadata["response_critique"]["attempts"] == 2


@pytest.mark.asyncio
async def test_critique_shadow_does_not_change_reply(monkeypatch):
    incoming = IncomingMessage(channel="whatsapp", text="me da o link")
    result = AgentResult(reply_text="sem link", intent="commerce")

    async def fake_judge(**kwargs):
        return CritiqueVerdict(
            score=10,
            pass_check=False,
            issues=["bad"],
            summary="bad",
            recommended_apis=[
                RecommendedApiCall(name="get_order_payment", arguments={"order_id": "1"})
            ],
        )

    monkeypatch.setattr("app.response_critique.run_critique_judge", fake_judge)

    final, report = await apply_response_critique_loop(
        incoming=incoming,
        result=result,
        mode="shadow",
        max_retries=2,
        execute=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no tools")),
    )
    assert final.reply_text == "sem link"
    assert report.regenerated is False
    assert report.approved is False
