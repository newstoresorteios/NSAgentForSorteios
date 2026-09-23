"""Incident regression: a catalog candidate is not proof of the photographed SKU."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from app.catalog.vision import catalog_evidence as resolver
from app.catalog.vision.prompt import ImageProductIdentification
from app.catalog.media.storefront_evidence import image_search_queries, product_page_evidence
from app.commerce.commerce_context import CommerceConversationState, CommerceProductReference
from app.models import IncomingMessage, AgentResult
from app.verify.catalog_delivery import enforce_photo_identity, validate_catalog_delivery, apply_output_style
from app.verify.final_response import finalize_response


def incoming():
    return IncomingMessage(text='quero esse', channel='whatsapp', input_modality='image',
                           image_url='https://media.example/photo', attachment_type='image')


def hypothesis(brand='Hamilton', model='Khaki Field Automatic', color='blue'):
    return ImageProductIdentification(is_watch=True, brand=brand, model=model, color=color, confidence=0.98)


@pytest.mark.parametrize('vision,query', [
    (hypothesis(), 'hamilton khaki field azul'),
    (hypothesis('Brew','Retrograph brown Chronograph','brown'), 'brew retrograph marrom'),
    (hypothesis('Marca Nova','Familia Nova Automatic','green'), 'marca nova familia verde'),
])
def test_queries_translate_colors_without_reference_overrides(vision, query):
    assert image_search_queries(vision)[0] == query


def test_queries_prioritize_bezel_color_without_watch_specific_rules():
    vision = hypothesis('Christopher Ward', 'C60 Trident', 'black')
    vision.dial_color = 'black'
    vision.bezel_color = 'red'

    assert image_search_queries(vision)[:3] == [
        'christopher ward c60 trident vermelho',
        'christopher ward vermelho',
        'christopher ward c60 trident preto',
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('brand,model,pid,name,ref', [
    ('Hamilton','Khaki Field Automatic','16010','Hamilton Khaki Field Murph Azul H70405740 38 mm','H70405740'),
    ('Brew','Retrograph brown','15998','Brew Retrograph Espresso Chronograph Meca-Quartz Marrom 38 mm',None),
    ('Marca Nova','Familia Automatic','99901','Marca Nova Familia Azul 38 mm','NOVA-38'),
])
async def test_photo_match_uses_page_identity_and_survives_reviewer_and_previous_sku(monkeypatch,brand,model,pid,name,ref):
    url='https://www.newstorerj.com.br/relogios/relogio-'+pid
    hit={'product_id':pid,'url':url,'_image_color_error':0.0004}
    monkeypatch.setattr(resolver,'search_storefront',AsyncMock(return_value=[hit]))
    monkeypatch.setattr(resolver,'rank_storefront_hits_by_image',AsyncMock(return_value=[(0,hit)]))
    page=AsyncMock(return_value={'id':pid,'url':url,'name':name,'reference':ref,'model':name,'brand':brand,'available':True})
    monkeypatch.setattr(resolver,'fetch_storefront_product',page)
    result=await resolver.resolve_catalog_photo(incoming(),hypothesis(brand,model))
    page.assert_awaited_once_with(url,expected_id=pid)
    # Replay the real failure: reviewer tries to replace a proven SKU with a sibling.
    result.reply_text='Pela imagem é o **Hamilton H70305143**, caixa de 40 mm.'
    previous=CommerceConversationState(active_product=CommerceProductReference(product_id='2721',name='Outro relógio'))
    fixed,state=finalize_response(result,incoming=incoming(),interpretation=None,previous_state=previous)
    assert name in fixed.reply_text and url in fixed.reply_text
    assert 'H70305143' not in fixed.reply_text and '*' not in fixed.reply_text
    assert [p.product_id for p in state.last_presented_products]==[pid]
    assert state.active_product is None
    assert state.active_preferences.get('subject_reference') == ref


@pytest.mark.asyncio
@pytest.mark.parametrize('distance,margin,color_error', [(22,10,.01),(0,0,.01),(0,0,.00001),(0,12,.25)])
async def test_text_similarity_or_ambiguous_photo_cannot_confirm_reference(monkeypatch,distance,margin,color_error):
    hit={'product_id':'2721','url':'https://www.newstorerj.com.br/relogios/relogio-errado','_image_color_error':color_error}
    monkeypatch.setattr(resolver,'search_storefront',AsyncMock(return_value=[hit]))
    monkeypatch.setattr(resolver,'rank_storefront_hits_by_image',AsyncMock(return_value=[(distance,hit),(distance+margin,{'product_id':'sibling'})]))
    page=AsyncMock(side_effect=AssertionError('An uncertain photo must not be promoted to a SKU'))
    monkeypatch.setattr(resolver,'fetch_storefront_product',page)
    result=await resolver.resolve_catalog_photo(incoming(),hypothesis())
    assert not result.commercial_data['products']
    result.reply_text='Pela imagem é o Hamilton H70305143'
    result=enforce_photo_identity(result)
    assert 'H70305143' not in result.reply_text
    assert result.response_metadata['clear_presented_products']
    page.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_competitor_image_prevents_false_uniqueness(monkeypatch):
    hit={'product_id':'1','_image_color_error':0.001}
    monkeypatch.setattr(resolver,'search_storefront',AsyncMock(return_value=[hit,{'product_id':'2'}]))
    monkeypatch.setattr(resolver,'rank_storefront_hits_by_image',AsyncMock(return_value=[(0,hit)]))
    page=AsyncMock(side_effect=AssertionError('Missing sibling evidence'))
    monkeypatch.setattr(resolver,'fetch_storefront_product',page)
    result=await resolver.resolve_catalog_photo(incoming(),hypothesis())
    assert not result.commercial_data['products']
    page.assert_not_awaited()


@pytest.mark.asyncio
async def test_identical_image_can_close_match_when_paginated_search_has_more(monkeypatch):
    from app.catalog.media.storefront_search import StorefrontSearchResults
    hit={'product_id':'16099','url':'https://www.newstorerj.com.br/relogios/relogio-correto','_image_color_error':0.0004}
    hits=StorefrontSearchResults([hit],complete=False)
    monkeypatch.setattr(resolver,'search_storefront',AsyncMock(return_value=hits))
    monkeypatch.setattr(resolver,'rank_storefront_hits_by_image',AsyncMock(return_value=[(0,hit)]))
    monkeypatch.setattr(resolver,'fetch_storefront_product',AsyncMock(return_value={
        'id':'16099','url':hit['url'],'name':'Produto visualmente idêntico',
        'reference':'REF-16099','model':'Modelo','brand':'Marca','available':True}))

    result=await resolver.resolve_catalog_photo(incoming(),hypothesis())

    assert result.commercial_data['match_status']=='exact'
    assert result.response_metadata['image_catalog_proof']['product_id']=='16099'


@pytest.mark.asyncio
async def test_image_route_never_calls_keyword_retrieval_to_invent_identity(monkeypatch):
    from app.catalog.vision import image_product_id
    from types import SimpleNamespace
    monkeypatch.setattr(image_product_id,'get_settings',lambda:SimpleNamespace(agent_image_search_enabled=True,agent_image_search_min_confidence=.55))
    monkeypatch.setattr(image_product_id,'identify_product_from_image',AsyncMock(return_value=hypothesis()))
    monkeypatch.setattr(image_product_id,'resolve_compiled_retrieval',lambda:pytest.fail('Text retrieval must not confirm a photographed SKU'))
    monkeypatch.setattr(resolver,'search_storefront',AsyncMock(return_value=[]))
    result=await image_product_id.handle_image_product_search(incoming())
    assert result.safety_reason=='image_catalog_unconfirmed'


def page_html(pid='15998', availability='YES'):
    row={'pageCategory':'Produto','idProduct':pid,'nameProduct':'Produto correto','urlProduct':'https://www.newstorerj.com.br/relogios/relogio-teste','availability':availability,'availabilityDetails':'Disponível em 30 dias úteis'}
    return '<div hidden>Produto Indisponível</div><script>dataLayer = '+json.dumps([row])+'</script>'


@pytest.mark.parametrize('flag,expected',[('YES',True),('NO',False),('',None)])
def test_availability_is_structured_not_hidden_modal(flag,expected):
    assert product_page_evidence(page_html(availability=flag),expected_id='15998')['available'] is expected
    assert product_page_evidence(page_html(),expected_id='2721') is None


@pytest.mark.asyncio
async def test_link_to_another_product_or_soft404_is_not_returned(monkeypatch):
    from app.catalog.media.product_media import ensure_product_has_live_url, official_product_url
    responses=[httpx.Response(200,text=page_html('999'),request=httpx.Request('GET','https://www.newstorerj.com.br/relogios/relogio-wrong')),
               httpx.Response(200,text='Produto não encontrado',request=httpx.Request('GET','https://www.newstorerj.com.br/sem-resultados-na-busca'))]
    client=AsyncMock()
    client.__aenter__.return_value=client
    client.get.side_effect=responses
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:client)
    for _ in range(2):
        product=await ensure_product_has_live_url({'id':'2721','url':'https://www.newstorerj.com.br/relogios/relogio-old'})
        assert product['_product_url_dead'] and official_product_url(product) is None
        assert product['url'] is None


@pytest.mark.asyncio
async def test_outbound_gate_rejects_link_even_when_api_supplied_it(monkeypatch):
    import app.verify.catalog_delivery as delivery
    url='https://www.newstorerj.com.br/relogios/relogio-hamilton-h70305143'
    probe=AsyncMock(return_value={'url':url,'_product_url_dead':True})
    monkeypatch.setattr(delivery,'ensure_product_has_live_url',probe)
    result=AgentResult(reply_text='Link oficial: '+url,intent='commerce',commercial_data={'product_link':{'product_id':'2721','product_url':url}})
    fixed=await validate_catalog_delivery(result)
    assert url not in fixed.reply_text
    assert fixed.commercial_data['product_link']['product_url'] is None
    assert fixed.response_metadata['rejected_catalog_urls']==[url]


@pytest.mark.asyncio
async def test_unbound_product_link_cannot_borrow_domain_trust():
    url='https://www.newstorerj.com.br/relogios/relogio-nao-consultado'
    result=AgentResult(reply_text='Link: '+url,intent='commerce')
    fixed=await validate_catalog_delivery(result)
    assert url not in fixed.reply_text
    assert fixed.safety_reason=='catalog_link_unverified'


@pytest.mark.asyncio
async def test_evidence_guard_avoids_paid_generic_review(monkeypatch):
    from app.verify import response_critique
    judge=AsyncMock(side_effect=AssertionError('Generic judge cannot validate visual identity'))
    monkeypatch.setattr(response_critique,'run_critique_judge',judge)
    result=AgentResult(reply_text='Preciso de mais evidência.',intent='commerce',response_metadata={'image_evidence_guard':True})
    fixed,report=await response_critique.apply_response_critique_loop(incoming=incoming(),result=result,mode='enforce')
    judge.assert_not_awaited()
    assert not report.regenerated


def test_plain_text_is_enforced_after_generation():
    result=AgentResult(reply_text='É o **modelo** com *38 mm*.',intent='commerce')
    assert '*' not in apply_output_style(result).reply_text


def test_incomplete_catalog_search_keeps_accurate_operator_message():
    result=AgentResult(
        reply_text='rascunho',
        intent='commerce',
        commercial_data={'products': []},
        response_metadata={
            'image_evidence_guard': True,
            'catalog_search_incomplete': True,
        },
    )

    fixed=enforce_photo_identity(result)

    assert fixed.safety_reason=='image_catalog_search_incomplete'
    assert 'busca em todo o cat' in fixed.reply_text


@pytest.mark.asyncio
async def test_catalog_search_reads_next_page_before_declaring_no_match(monkeypatch):
    from app.catalog.media.storefront_search import search_storefront
    def html(pid,next_page=False):
        row={'idProduct':pid,'nameProduct':'Modelo '+pid,'urlProduct':'https://www.newstorerj.com.br/relogios/relogio-'+pid}
        return ('<link rel="next" href="?pg=2">' if next_page else '')+'<script>dataLayer = '+json.dumps([{'listProducts':[row],'filter':{}}])+'</script>'
    client=AsyncMock()
    client.__aenter__.return_value=client
    client.get.side_effect=[httpx.Response(200,text=html('other',True)),httpx.Response(200,text=html('16010'))]
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:client)
    hits=await search_storefront('familia azul',max_pages=3,limit=36)
    assert [h['product_id'] for h in hits]==['other','16010']
    assert 'pg=2' in client.get.call_args_list[1].args[0]


@pytest.mark.asyncio
async def test_complete_response_pipeline_preserves_photo_proof(monkeypatch):
    from types import SimpleNamespace
    import app.message_pipeline as pipeline
    url='https://www.newstorerj.com.br/relogios/relogio-murph-h70405740'
    hit={'product_id':'16010','url':url,'_image_color_error':0.0004}
    monkeypatch.setattr(resolver,'search_storefront',AsyncMock(return_value=[hit]))
    monkeypatch.setattr(resolver,'rank_storefront_hits_by_image',AsyncMock(return_value=[(0,hit)]))
    monkeypatch.setattr(resolver,'fetch_storefront_product',AsyncMock(return_value={
        'id':'16010','url':url,'name':'Hamilton Khaki Field Murph H70405740 38 mm',
        'reference':'H70405740','model':'Khaki Field Murph','brand':'Hamilton','available':True}))
    photo=await resolver.resolve_catalog_photo(incoming(),hypothesis())
    monkeypatch.setattr(pipeline,'generate_agent_reply_async',AsyncMock(return_value=photo))
    monkeypatch.setattr(pipeline,'get_settings',lambda:SimpleNamespace(
        audio_inbound_enabled=False,audio_outbound_enabled=False,agent_policy_mode='shadow',
        agent_factual_validation_mode='enforce',agent_trusted_fact_domains='newstorerj.com.br',
        agent_critique_mode='enforce',agent_quality_judge_mode='enforce',agent_double_check_mode='enforce',
        agent_emergency_rollback=False,max_reply_chars=900,agent_persona_tenant_id='newstore'))
    monkeypatch.setattr(pipeline,'load_commerce_conversation_state',lambda **kw:{})
    monkeypatch.setattr(pipeline,'persist_customer_commerce_session',lambda **kw:None)
    monkeypatch.setattr(pipeline,'upsert_customer_identity_links',lambda *a,**kw:None)
    result=await pipeline._process_incoming_message(incoming(),{})
    assert 'H70405740' in result.reply_text and url in result.reply_text
    assert not result.handoff_required
    assert result.response_metadata['final_response_validation']['delivered_product_ids']==['16010']
