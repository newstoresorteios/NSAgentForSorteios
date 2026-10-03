import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.direct.agent import DirectOpenAIAgent
from app.direct.tools import DirectTools
from app.direct.history import load_history
from app.models import IncomingMessage
from tests.direct.test_direct_agent import client, response, settings


@pytest.mark.asyncio
async def test_two_pages_retain_total_and_images_without_five_item_cutoff():
    def page(offset):
        return {'total': 23, 'returned': 10, 'has_more': True, 'next_offset': offset+10,
                'offset': offset, 'products': [{'name': f'Model {n}', 'url': f'https://www.newstorerj.com/{n}',
                'image_url': f'https://images.tcdn.com.br/{n}.jpg'} for n in range(offset,offset+10)]}
    adapter=NS(search_ready_delivery=AsyncMock(side_effect=[page(0),page(10)]))
    tools=DirectTools(incoming=IncomingMessage(), history=[], documents=[], adapter=adapter)
    first=await tools.execute('search_ready_delivery', '{"query":"pronta entrega","offset":0,"limit":10}')
    second=await tools.execute('search_ready_delivery', '{"query":"pronta entrega","offset":10,"limit":10}')
    assert len(first['data']['products']) == len(second['data']['products']) == 10
    assert second['data']['total'] == 23 and second['data']['next_offset'] == 20
    assert len(tools.products)==20
    adapter.search_ready_delivery.assert_awaited_with('pronta entrega',offset=10,limit=10,snapshot_id=None)


@pytest.mark.asyncio
async def test_rebuilt_conversation_keeps_recommended_product_and_attaches_verified_photo():
    product={'name':'Longines Spirit Azul L3.410.4.93.0','url':'https://www.newstorerj.com/longines',
             'image_url':'https://images.tcdn.com.br/longines.jpg','source':'https://www.newstorerj.com/pronta-entrega'}
    history=[{'role':'user','content':'Tenho um casamento'},
             {'role':'assistant','content':'Sugiro o Longines Spirit Azul L3.410.4.93.0.'}]
    incoming=IncomingMessage(provider='brevo',channel='whatsapp',sender_key='same-person',visitor_id='same-visitor',conversation_id='new-thread',text='me manda a foto dele')
    tools=DirectTools(incoming=incoming,history=history,documents=[],products=[product])
    call=NS(type='function_call',id='fc',call_id='c',name='prepare_product_image',arguments=json.dumps({'product_url':product['url']}))
    api=client([response(calls=[call]),response('Aqui está a foto do Longines que sugeri.')])
    result=await DirectOpenAIAgent(api,settings()).run_turn(incoming=incoming,workspace_id='w',history=history,
        previous={'direct_agent':{'scope':'old-thread'}},tools=tools,content=[{'type':'input_text','text':incoming.text}])
    assert api.conversations.create.await_args.kwargs['items']==history
    assert product['name'] in api.responses.create.await_args.kwargs['instructions']
    assert result.response_metadata['outbound_image_url']==product['image_url']
    assert result.response_metadata['direct_agent']['products']==[product]


@pytest.mark.asyncio
async def test_unknown_image_and_external_hosts_are_not_sent():
    tools=DirectTools(incoming=IncomingMessage(),history=[],documents=[],products=[
        {'url':'https://www.newstorerj.com/p','image_url':'https://evil.test/a.jpg'}])
    for url in ('https://www.newstorerj.com/unknown','https://www.newstorerj.com/p'):
        assert not (await tools.execute('prepare_product_image',json.dumps({'product_url':url})))['ok']
    assert tools.outbound_image_url is None
    assert tools.outbound_image_urls == []


@pytest.mark.asyncio
async def test_several_verified_photos_are_kept():
    products = [
        {'name': f'Model {n}', 'url': f'https://www.newstorerj.com/{n}',
         'image_url': f'https://images.tcdn.com.br/{n}.jpg'}
        for n in range(2)
    ]
    tools = DirectTools(incoming=IncomingMessage(), history=[], documents=[], products=products)
    first = await tools.execute('prepare_product_image', json.dumps({'product_url': products[0]['url']}))
    second = await tools.execute('prepare_product_image', json.dumps({'product_url': products[1]['url']}))
    assert first['ok'] and second['ok'] and second['attached'] == 1
    assert tools.outbound_image_urls == [products[0]['image_url'], products[1]['image_url']]


@pytest.mark.asyncio
async def test_snapshot_expiry_requests_refresh_not_no_stock():
    from app.tray.tray_adapter_client import TrayAdapterError
    adapter=NS(search_ready_delivery=AsyncMock(side_effect=TrayAdapterError('expired',status_code=409)))
    tools=DirectTools(incoming=IncomingMessage(),history=[],documents=[],adapter=adapter)
    result=await tools.execute('search_ready_delivery','{"query":"pronta entrega","offset":10,"snapshot_id":"'+('a'*32)+'"}')
    assert result['error']=='catalog_snapshot_expired'
    adapter.search_ready_delivery.assert_awaited_with('pronta entrega',offset=10,limit=10,snapshot_id='a'*32)


@pytest.mark.parametrize('provider,channel,visitor,allowed',[
    ('brevo','whatsapp','same-visitor',True),('brevo','whatsapp',None,False),('meta','instagram','visitor',False)])
def test_thread_rollover_is_bound_to_workspace_channel_sender_and_visitor(monkeypatch,provider,channel,visitor,allowed):
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,sql,params): self.sql,self.params=sql,params
        def fetchall(self): return []
    cursor=Cursor()
    class Conn:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def cursor(self): return cursor
    monkeypatch.setattr('app.direct.history.get_conn',lambda:Conn())
    load_history(IncomingMessage(provider=provider,channel=channel,sender_key='same-person',visitor_id=visitor,conversation_id='new-thread'),'workspace')
    assert cursor.params['brevo_continuity'] is allowed
    assert cursor.params['identity']=='same-person' and cursor.params['workspace']=='workspace'
    assert 'i.visitor_id=%(visitor)s' in cursor.sql and "interval '24 hours'" in cursor.sql
    assert 'provider_send_ok=true' in cursor.sql
