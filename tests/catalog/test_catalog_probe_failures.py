import pytest
from unittest.mock import AsyncMock
from app.models import SalesInterpretation, AgentResult
from app.catalog.retrieval.tokens import effective_product_reference
from app.catalog.retrieval.compiler import ProductRetrievalCompiler
from app.catalog.specs.catalog_specs import (extract_case_size_mm,
    extract_case_size_range_from_text, product_matches_case_size_range)


def interpretation(**kwargs):
    return SalesInterpretation(references_previous_context=False,needs_clarification=False,confidence=.99,**kwargs)


@pytest.mark.asyncio
async def test_inspecting_budget_in_pix_does_not_become_payment_policy():
    from app.sales.catalog_purchase import try_catalog_purchase
    from app.commerce.commerce_context import CommerceConversationState
    from app.models import IncomingMessage
    i = interpretation(domain='commerce', goal='inspect', subject={'reference':'SRPL13K1'},
                       preferences={'budget_max':5000}, payment_method_preference='pix')
    result = await try_catalog_purchase(message=IncomingMessage(text='Ele cabe em R$ 5.000 no Pix?',
        conversation_id='budget-test'), interpretation=i, plan={'goal':'inspect'},
        state=CommerceConversationState(), purchase_action=None, resolved_product=None,
        purchase_requests=[], unresolved_purchase_items=0, unresolved_candidates=[])
    assert result is None


@pytest.mark.asyncio
async def test_inspection_purchase_items_do_not_authorize_cart():
    from app.sales.catalog_pending import apply_catalog_pending
    from app.commerce.commerce_context import CommerceConversationState
    from app.models import IncomingMessage
    i = interpretation(domain='commerce', goal='inspect', subject={'reference':'SRPL13K1'},
        preferences={'budget_max':5000}, payment_method_preference='pix',
        purchase_items=[{'quantity':1,'reference_type':'explicit_product','explicit_product_name':'Seiko SRPL13K1'}])
    result = await apply_catalog_pending(message=IncomingMessage(text='Ele cabe no orçamento?',
        conversation_id='budget-test'), interpretation=i, plan={'goal':'inspect'},
        state=CommerceConversationState(), purchase_action=None, resolved_product=None, resolved_by='none')
    assert not result.purchase_requests
    assert result.purchase_action is None


@pytest.mark.asyncio
async def test_explicit_inspection_does_not_reopen_shortlist_selection():
    from app.sales.catalog_reference import resolve_catalog_reference
    from app.commerce.commerce_context import CommerceConversationState
    from app.models import IncomingMessage
    i = interpretation(domain='commerce',goal='inspect',subject={'reference':'SRPL13K1'},
        preferences={'color':'azul'},reference_type='current_product')
    result = await resolve_catalog_reference(message=IncomingMessage(
        text='O SRPL13K1 serve para mostrador azul?',conversation_id='color-test'),
        interpretation=i,plan={'goal':'inspect'},state=CommerceConversationState())
    assert result.early_result is None
    assert result.resolved_by=='explicit_identity_inspection'
    assert result.resolved_product is None


@pytest.mark.parametrize('reference', ['SRPL13K1', 'SPB155', 'SRPE33', 'SNK361'])
def test_compact_reference_compiles_as_exact(reference):
    assert effective_product_reference(reference) == reference
    plan = ProductRetrievalCompiler.compile(interpretation(domain='commerce', goal='inspect',
                                           subject={'reference': reference}))
    assert plan.requests[0].tool_arguments()['reference'] == reference


@pytest.mark.parametrize('text', ['4R35', '200m', '41mm', 'preto', 'aço'])
def test_measurements_and_calibre_are_not_references(text):
    assert effective_product_reference(text) is None


def test_negated_eco_drive_does_not_become_solar_requirement():
    from app.catalog.specs.requirements import normalize_requirements
    i=interpretation(domain='commerce',goal='recommend',subject={'brand':'Citizen'},
        preferences={'mechanism':'automatic|solar','color':'preto'})
    assert normalize_requirements(i,'Quero automático, não Eco-Drive.')['mechanism']=='automatic'
    plan=ProductRetrievalCompiler.compile(i)
    targeted=next(r for r in plan.requests if r.strategy=='technical_identity_probe')
    assert targeted.name=='automático preto' and targeted.brand=='Citizen'
    i.subject.model='Promaster'
    plan=ProductRetrievalCompiler.compile(i)
    targeted=next(r for r in plan.requests if r.strategy=='technical_identity_probe')
    assert targeted.name=='Promaster Automático Preto'


@pytest.mark.asyncio
async def test_technical_probe_does_not_require_sparse_color_property():
    from app.catalog.retrieval.session import RetrievalSession
    from app.catalog.retrieval.probes import run_probes
    i=interpretation(domain='commerce',goal='recommend',subject={'brand':'Citizen'},
        preferences={'mechanism':'automatic','color':'preto','budget_max':3500})
    plan=ProductRetrievalCompiler.compile(i)
    tool=AsyncMock(return_value={'products':[]})
    session=RetrievalSession(i,plan,None,tool,search_call_limit=1,
        list_query_extras=lambda _: {'property_name':'Cor','property_value':'Preto','current_price_range':'0,3500'})
    await run_probes(session,[r for r in plan.requests if r.strategy=='technical_identity_probe'])
    args=tool.await_args.args[1]
    assert args['brand']=='Citizen' and 'current_price_range' not in args
    assert 'property_name' not in args and 'property_value' not in args
    assert args['tokens']==['automático','preto'] and 'name' not in args


def test_decimal_case_diameter_is_not_lug_to_lug_or_strap_width():
    description = 'Espessura: 12,2 mm. Lug to lug: 49,5 mm. Tamanho da Caixa: 41,7 mm. Pulseira: 20 mm.'
    assert extract_case_size_mm({'description':description}) == '41.7'
    assert extract_case_size_range_from_text('Quero caixa de 41,7 mm, pulseira de 20 mm, espessura 12,2 mm') == (41.7,41.7)
    assert product_matches_case_size_range({'description':description},41.7,41.7)
    assert not product_matches_case_size_range({'description':description},38,38)


@pytest.mark.parametrize('mode', ['exact', 'recommendation'])
def test_technical_probe_candidates_still_require_current_price_under_budget(mode):
    from app.catalog.retrieval.hard_filter import hard_filter_products
    i = interpretation(domain='commerce', goal='recommend', subject={'brand':'Citizen'},
                       preferences={'budget_max':3500})
    product = {'id':'test', 'name':'Citizen Promaster', 'brand':'Citizen',
               'price':4399.99, 'current_price':3399.99, 'available':'1'}
    assert hard_filter_products([product], i, mode=mode) == [product]
    assert not hard_filter_products([{**product, 'current_price':3599.99}], i, mode=mode)


@pytest.mark.asyncio
async def test_inspection_resolves_reference_without_contradictory_filters(monkeypatch):
    from app.catalog.retrieval import executor
    tool = AsyncMock(return_value={'products':[{'id':'11989','reference':'SRPL13K1','brand':'Seiko'}]})
    detail = AsyncMock(return_value=AgentResult(reply_text='Hardlex',intent='commerce'))
    monkeypatch.setattr(executor, 'execute_contextual_product_lookup', detail)
    i=interpretation(domain='commerce',goal='inspect',subject={'reference':'SRPL13K1'},
                         preferences={'crystal':'sapphire','budget_max':5000,'attributes':['case_size:38mm']})
    result=await executor._execute_compiled_product_retrieval_unlocked(i,execute_tool=tool)
    tool.assert_awaited_once_with('search_products',{'reference':'SRPL13K1','limit':5,'page':1})
    assert detail.await_args.args[1].product_id=='11989'
    assert result.response_metadata['identity_inspection']


@pytest.mark.asyncio
async def test_inspection_never_substitutes_another_reference():
    from app.catalog.retrieval import executor
    tool=AsyncMock(return_value={'products':[{'id':'other','reference':'SPB155'}]})
    i=interpretation(domain='commerce',goal='inspect',subject={'reference':'SRPL13K1'})
    result=await executor._execute_compiled_product_retrieval_unlocked(i,execute_tool=tool)
    assert result.safety_reason=='product_not_found'
    assert tool.await_count==1


@pytest.mark.asyncio
async def test_inspection_surfaces_budget_and_catalog_discrepancy(monkeypatch):
    from app.catalog.retrieval import executor
    from app.commerce.commerce_context import CommerceProductReference
    product = {'id':'11989', 'name':'Seiko', 'reference':'SRPL13K1',
        'price':6099.99, 'pix_price':5184.99, 'stock':1, 'available':True,
        'description':'Tamanho da Caixa: 41,7 mm', 'properties':{'Tamanho da caixa':['41mm']}}
    monkeypatch.setattr(executor, 'enrich_product_variants', AsyncMock(side_effect=lambda p,*a:p))
    i = interpretation(domain='commerce', goal='inspect',subject={'reference':'SRPL13K1'},
        preferences={'budget_max':5000},payment_method_preference='pix')
    result = await executor.execute_contextual_product_lookup(i,
        CommerceProductReference(product_id='11989'),execute_tool=AsyncMock(return_value=product))
    assert result.response_metadata['identity_inspection'] is True
    assert result.commercial_data['inspection_budget']['within_budget'] is False
    assert result.commercial_data['catalog_discrepancies'][0]['description']=='41.7'
    assert result.commercial_data['catalog_discrepancies'][0]['summary']=='41'


@pytest.mark.asyncio
@pytest.mark.parametrize('calibre,text',[('4R35','Seiko Samurai automático 4R35 com Hardlex'),
                                       ('8204','Citizen automático, calibre 8204, mineral')])
async def test_calibre_in_technical_prose_is_not_used_as_product_reference(monkeypatch,calibre,text):
    from app.catalog.retrieval import executor
    from types import SimpleNamespace
    i=interpretation(domain='commerce',goal='find',subject={'brand':'Seiko','model':'Samurai','reference':calibre})
    seen=[]
    def compile_plan(interp, **kwargs):
        seen.append(interp.subject.reference)
        return SimpleNamespace(mode='exact',requests=[],candidate_limit=20)
    monkeypatch.setattr(executor.ProductRetrievalCompiler,'compile',compile_plan)
    await executor._execute_compiled_product_retrieval_unlocked(i,message_text=text)
    assert seen and all(value is None for value in seen)
    assert 'caliber:'+calibre in i.preferences.attributes
