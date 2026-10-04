import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.models import IncomingMessage
from app.direct.continuity import save_preference
from app.direct.knowledge import search_knowledge, read_document, document_id
from app.direct.tools import DirectTools


@pytest.mark.parametrize('text,evidence,error', [
    ('não gosto de dourado', 'gosto de dourado', 'negation_must_be_preserved'),
    ('Oi', 'Prefiro azul', 'current_message_evidence_required'),
    ('Meu amigo prefere azul', 'Meu amigo prefere azul', 'explicit_preference_required'),
    ('Eu quero guardar minha senha 123', 'Eu quero guardar minha senha 123', 'memory_policy_rejected'),
    ('Eu quero lembrar que custa R$ 500', 'Eu quero lembrar que custa R$ 500', 'memory_policy_rejected'),
])
def test_memory_requires_current_non_sensitive_explicit_evidence(text,evidence,error):
    result=save_preference(incoming=IncomingMessage(text=text,sender_key='customer'),workspace='w',tenant='t',
        key='preferred_color',action='save',evidence=evidence,preview=True,state={})
    assert result['error']==error


def test_preview_memory_correction_and_forget_never_access_database(monkeypatch):
    db=Mock(side_effect=AssertionError('no database in preview'))
    monkeypatch.setattr('app.memory.contact_memory_repository.get_active_contact_memories',db)
    state={}
    for action,text in [('save','Eu prefiro azul'),('save','Agora eu prefiro verde'),('forget','Esqueça minha cor preferida')]:
        result=save_preference(incoming=IncomingMessage(text=text,sender_key='a'),workspace='w',tenant='t',
            key='preferred_color',action=action,evidence=text,preview=True,state=state)
        assert result['ok']
        assert state==({'preferred_color':text} if action=='save' else {})
    db.assert_not_called()


def test_durable_preference_scopes_write_and_removes_legacy_alias(monkeypatch):
    import app.memory.contact_memory_repository as repo
    import app.config
    monkeypatch.setattr(app.config, 'get_settings', lambda:NS(direct_memory_enabled=True))
    monkeypatch.setattr(repo,'get_active_contact_memories',Mock(return_value=[]))
    save=Mock(); forget=Mock()
    monkeypatch.setattr(repo,'upsert_contact_memory',save)
    monkeypatch.setattr(repo,'forget_contact_memory',forget)
    text='Não gosto de dourado, prefiro azul'
    r=save_preference(incoming=IncomingMessage(text=text,sender_key='customer',raw={'inbound_id':17}),
        workspace='workspace',tenant='tenant',key='preferred_color',action='save',evidence=text)
    assert r['ok']
    assert save.call_args.kwargs['workspace_id']=='workspace'
    assert save.call_args.kwargs['sender_key']=='customer'
    assert save.call_args.kwargs['value']==text
    assert save.call_args.kwargs['source_inbound_id']==17
    assert forget.call_args.kwargs['memory_key']=='color_preference'


def test_knowledge_synonyms_and_full_document_version():
    docs=[dict(title='Garantia',source='published',text='Assistência e reparo de defeito de fabricação. '+('Detalhes adicionais. '*1000))]
    r=search_knowledge(docs,'Meu relógio quebrou')
    assert r['documents']
    identity=r['documents'][0]['document_id']
    page=read_document(docs,identity)
    assert page['next_offset']==8000
    assert read_document(docs,identity,8000)['text']==docs[0]['text'][8000:16000]
    docs[0]['text']+='Nova política'
    assert not read_document(docs,identity)['ok']


@pytest.mark.asyncio
async def test_shipping_uses_verified_price_and_filters_output():
    adapter=NS(get_product=AsyncMock(return_value={'product':{'id':'4','current_price':'1200.00'}}),
        quote_shipping=AsyncMock(return_value={'success':True,'options':[{'name':'PAC','price':'35','max_period':5,'secret':'hidden'}]}))
    from app.direct.commercial import quote_shipping
    args=NS(product_id='4',variant_id=None,zipcode='22000-000',quantity=1)
    incoming=IncomingMessage(text='Meu CEP 22000-000')
    result=await quote_shipping(adapter,args,incoming,[],{'4'})
    assert result['ok'] and 'secret' not in json.dumps(result)
    adapter.quote_shipping.assert_awaited_once_with(zipcode='22000000',products=[{'product_id':4,'price':'1200.00','quantity':1}])
    adapter.get_product.reset_mock()
    assert not (await quote_shipping(adapter,args,incoming,[],set()))['ok']
    adapter.get_product.assert_not_awaited()


@pytest.mark.asyncio
async def test_shipping_rejects_variant_of_another_product():
    from app.direct.commercial import quote_shipping
    adapter=NS(get_product=AsyncMock(return_value={'product':{'id':'4','price':100,'has_variation':True}}),
        get_product_variant=AsyncMock(return_value={'variant':{'id':'5','product_id':'9','price':100}}),quote_shipping=AsyncMock())
    result=await quote_shipping(adapter,NS(product_id='4',variant_id='5',zipcode='22000000',quantity=1),
                                IncomingMessage(text='22000000'),[],{'4'})
    assert result['error']=='variant_identity_mismatch'
    adapter.quote_shipping.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_adapter_routes_match_http_contract():
    from app.tray.tray_adapter_client import TrayAdapterClient
    from app.tray.tray_circuit_breaker import reset_tray_circuit_breaker_for_tests
    reset_tray_circuit_breaker_for_tests()
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(200,json={'success':True,'product':{'url':'https://www.newstorerj.com/p'},'variants':[],'options':[]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond),timeout=12) as http:
        adapter=TrayAdapterClient('https://adapter.test','test-token',http)
        await adapter.get_ready_delivery_details(url='https://www.newstorerj.com/p',snapshot_id='a'*32)
        await adapter.list_product_variants('4')
        await adapter.quote_shipping(zipcode='22000000',products=[{'product_id':4,'price':'100','quantity':1}])
    assert [(r.method,r.url.path) for r in requests]==[('GET','/internal/ready-delivery/product'),('GET','/internal/products/variants'),('POST','/internal/shippings/quote')]
    assert dict(requests[0].url.params)=={'url':'https://www.newstorerj.com/p','snapshot_id':'a'*32}
    assert dict(requests[1].url.params)=={'product_id':'4'}
    assert json.loads(requests[2].content)=={'zipcode':'22000000','products':[{'product_id':4,'price':'100','quantity':1}]}
    assert all(r.headers['Authorization']=='Bearer test-token' and r.extensions['timeout']['read']==12 for r in requests)
