from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.models import IncomingMessage
from app.persona.institutional_route import institutional_question, answer_institutional
from app.verify.guardrails import detect_trade_in_or_appraisal_request

@pytest.mark.parametrize('text', ['Qual é a politica de troca dos relógios?', 'Qual a política de troca?', 'Como funciona a devolução?', 'Bom dia! Como funciona a troca?'])
def test_policy_is_not_trade_in(text):
    assert institutional_question(text)
    assert not detect_trade_in_or_appraisal_request(text)

@pytest.mark.parametrize('text', ['Quero avaliar meu relógio usado', 'Quero vender meu relógio', 'Vocês compram relógios usados?'])
def test_actual_appraisal_remains_human(text):
    assert not institutional_question(text)
    from app.sales.conversation_preflight import preflight_reply
    assert detect_trade_in_or_appraisal_request(text) or preflight_reply(text) is not None

@pytest.mark.asyncio
async def test_published_documents_reach_writer_and_trace(monkeypatch):
    from app.persona import institutional_route
    from app.agents import door
    from app.commerce.commerce_context import CommerceConversationState
    from app.verify.final_response import finalize_response
    from app.sales.answer_council import apply_answer_council_with_retry
    monkeypatch.setattr('app.config.get_settings', lambda: SimpleNamespace(openai_api_key='test',openai_model='test-model'))
    captured={}
    def compiler(**kw):
        captured.update(kw)
        return 'published policy'
    monkeypatch.setattr('app.llm.prompt_compiler.resolve_system_instructions', compiler)
    model=AsyncMock(return_value=SimpleNamespace(text='A política prevê 30 dias corridos. Consulte a política oficial.'))
    monkeypatch.setattr('app.llm.openai_gateway.generate_text_output',model)
    incoming=IncomingMessage(text='Qual é a politica de troca dos relógios?')
    result=await door._generate_agent_reply_async_inner(incoming, {})
    assert result.response_metadata['domain']=='institutional'
    assert any(d.get('slug')=='trocas-e-devolucoes' for d in captured['relevant_knowledge'])
    assert any('30 dias corridos' in d['body'] for d in captured['relevant_knowledge'])
    previous=CommerceConversationState(order_id='old-order')
    result,decision,_=await apply_answer_council_with_retry(result,incoming=incoming,interpretation=None,commerce_state=previous)
    final,state=finalize_response(result,incoming=incoming,interpretation=None,previous_state=previous)
    assert '30 dias' in final.reply_text
    assert not final.handoff_required
    assert state.order_id=='old-order'
    assert state is not previous
    assert final.response_metadata['institutional_evidence'][0]['content_hash']
    model.assert_awaited_once()

@pytest.mark.asyncio
async def test_no_key_returns_sources_without_paid_call(monkeypatch):
    monkeypatch.setattr('app.config.get_settings',lambda:SimpleNamespace(openai_api_key=''))
    result=await answer_institutional(IncomingMessage(text='Qual a politica de troca?'))
    assert 'politica-de-troca-e-devolucao' in result.reply_text
    assert not result.response_metadata['used_openai_responder']
