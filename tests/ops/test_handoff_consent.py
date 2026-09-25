import pytest

from app.models import IncomingMessage, AgentResult
from app.ops.handoff_consent import customer_requests_human, consent_reason, offer_text
from app.ops.handoff_service import enrich_handoff_metadata, handoff_provider_payload, is_handoff_acceptance


def test_consent_recognition_and_offer_follow_operator_configuration():
    import json
    from copy import deepcopy
    from app.configuration.runtime import current_bundle,bind_bundle,reset_bundle
    bundle=deepcopy(current_bundle())
    rules=json.loads(bundle['values']['handoffConsentRules'])
    rules['acceptance']='aceito o encaminhamento'
    bundle['values']['handoffConsentRules']=json.dumps(rules)
    bundle['values']['message.handoff_offer']='Mensagem personalizada do operador.'
    token=bind_bundle(bundle,None)
    try:
        assert offer_text()=='Mensagem personalizada do operador.'
        history=[{'role':'assistant','content':offer_text(),'metadata':{'handoff':{'offer':True,'required':False}}}]
        assert is_handoff_acceptance('Aceito o encaminhamento',history)
        assert not is_handoff_acceptance('sim',history)
    finally:
        reset_bundle(token)


@pytest.mark.parametrize('text', ['Preciso de ajuda', 'Qual o telefone da loja?', 'Estou falando com um robô?',
    'Não quero falar com atendente', 'Não me transfira para um atendente', 'Se precisar vou falar com atendente', 'Quero continuar sem um atendente', 'Pode encaminhar o link?', 'Qual o prazo de entrega?'])
def test_ordinary_messages_and_refusals_are_not_direct_requests(text):
    assert not customer_requests_human(text)


@pytest.mark.parametrize('text', ['Quero falar com um atendente', 'Me transfere para a equipe',
    'Gostaria de falar com uma pessoa', 'Quero um humano', 'Não consigo comprar, quero falar com atendente', 'Chame um atendente', 'atendente', 'falar com vendas',
    'Terei que falar com atendente físico.', 'Vou ter que conversar com uma pessoa'])
def test_explicit_customer_requests(text):
    assert customer_requests_human(text)
    incoming = IncomingMessage(channel='whatsapp', text=text)
    result = enrich_handoff_metadata(incoming, AgentResult(reply_text='Outra resposta', intent='support'))
    assert result.handoff_required
    assert handoff_provider_payload(result)['consent_reason'] == 'customer_requested_human'


@pytest.mark.parametrize('text', ['sim', 'pode encaminhar', 'por favor', 'sim, pode chamar'])
def test_acceptance_requires_immediately_preceding_human_offer(text):
    offer = [{'role':'assistant','content': offer_text()}]
    assert is_handoff_acceptance(text, offer)
    assert not is_handoff_acceptance(text, [{'role':'assistant','content':'Quer que eu encaminhe o link do relógio?'}])
    assert not is_handoff_acceptance(text, [{'role':'assistant','content':'A equipe de vendas atende na loja.'}])
    assert not is_handoff_acceptance(text, offer + [{'role':'user','content':'Não quero'}])
    assert not is_handoff_acceptance(text, offer + [{'role':'assistant','content':'Quer ver outro modelo?'}])


@pytest.mark.parametrize('text', ['não', 'agora não', 'sim, mas não quero atendente', 'por favor, continue você', 'quero ver outro relógio'])
def test_ambiguous_or_negative_answer_is_not_consent(text):
    assert not is_handoff_acceptance(text, [{'role':'assistant','content':offer_text()}])


def test_failure_offer_then_customer_acceptance_enters_queue_only_on_second_turn():
    first = enrich_handoff_metadata(IncomingMessage(channel='whatsapp',text='Qual o frete?'),
        AgentResult(reply_text='Encaminhei você',intent='shipping',handoff_required=True,safety_reason='shipping_failed'))
    assert not first.handoff_required
    assert first.response_metadata['handoff']['offer']
    assert handoff_provider_payload(first) is None
    history = [{'role':'assistant','content':first.reply_text,'metadata':first.response_metadata}]
    second = enrich_handoff_metadata(IncomingMessage(channel='whatsapp',text='sim'),
        AgentResult(reply_text='Tudo certo',intent='support'),recent_turns=history)
    assert second.handoff_required
    assert handoff_provider_payload(second)['consent_reason'] == 'customer_accepted_handoff_offer'


def test_result_flags_cannot_manufacture_customer_consent():
    result = AgentResult(reply_text='Transferido',intent='handoff',handoff_required=True,
        safety_reason='customer_accepted_handoff_offer',response_metadata={'handoff':{'confirmed':True,'required':True}})
    result = enrich_handoff_metadata(IncomingMessage(channel='whatsapp',text='Qual o preço?'),result)
    assert not result.handoff_required
    assert handoff_provider_payload(result) is None


def test_consent_history_requires_exact_inbound_identity(monkeypatch):
    from app.ops import handoff_consent
    calls = []
    monkeypatch.setattr(handoff_consent,'delivered_handoff_turns',lambda *args: calls.append(args) or [])
    incoming = IncomingMessage(channel='whatsapp',conversation_id='current-thread',text='sim',raw={'inbound_id':123})
    assert consent_reason(incoming) is None
    assert calls == [(123,'current-thread','whatsapp')]
    calls.clear()
    assert consent_reason(IncomingMessage(channel='whatsapp',sender_phone='same-phone',text='sim')) is None
    assert calls == []
def test_declining_immediate_offer_explains_continuation_without_transfer():
    from app.agents.door_gates import try_accepted_handoff
    from app.models import IncomingMessage
    result=try_accepted_handoff(IncomingMessage(text='Não precisa, quero continuar por aqui.'),
        [{'role':'assistant','content':'Quer que eu encaminhe para um atendente?',
          'metadata':{'handoff':{'offer':True,'required':False}}}])
    assert result is not None and not result.handoff_required
    assert result.response_metadata['handoff']['declined']
    assert 'devolução' in result.reply_text and 'nome' not in result.reply_text


def test_unflagged_model_transfer_promise_becomes_an_offer():
    from app.ops.handoff_service import enrich_handoff_metadata
    from app.models import IncomingMessage,AgentResult
    result=enrich_handoff_metadata(IncomingMessage(text='Não entendeu'),
        AgentResult(reply_text='Vou te passar para o João da equipe.',intent='commerce'),recent_turns=[])
    assert not result.handoff_required and result.response_metadata['handoff']['offer']
    assert 'Quer que' in result.reply_text
