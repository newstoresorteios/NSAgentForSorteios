from app.models import AgentResult
from app.catalog.retrieval.offer_contract import seal_offer, enforce_offer
from app.verify.fact_authority import authorize_products_for_responder
from tests.catalog.test_catalog_probe_failures import interpretation


def test_authorized_projection_does_not_erase_a_valid_offer():
    product={'id':'1','name':'Seiko','brand':'Seiko','price':2000,'stock':2,'available':True,
             'description':'Automático com Hardlex','images':[{'url':'https://example.com/1.jpg'}],
             'category_name':'Relógios','payment_option':'12x sem juros',
             '_revalidated':True,'_factual_source':'tray_live'}
    result=seal_offer(AgentResult(reply_text='Seiko custa R$ 2.000.',intent='commerce',commercial_data={'products':[product]}))
    result.commercial_data['products'],_=authorize_products_for_responder([product])
    i=interpretation(domain='commerce',goal='find',subject={'brand':'Seiko'})
    assert enforce_offer(result,i).reply_text=='Seiko custa R$ 2.000.'
    altered=result.model_copy(deep=True)
    altered.commercial_data['products'][0]['price']=1
    assert enforce_offer(altered,i).safety_reason=='catalog_requirements_unknown'


def test_negative_inspection_is_not_an_over_budget_offer():
    from app.sales.answer_council import check_pedido,check_fatos
    from app.sales.turn_contract import TurnContract
    contract=TurnContract(budget_max=5000)
    result=AgentResult(reply_text='Não cabe no orçamento: o preço é R$ 6.099,99.',intent='commerce',
        commercial_data={'products':[{'id':'1','name':'Seiko','price':6099.99,'_revalidated':True}]},
        response_metadata={'identity_inspection':True})
    assert check_pedido(result,contract).pass_check and check_fatos(result,contract).pass_check
    result.reply_text = 'No Pix fica R$ 5.184,99, então não cabe no teto de R$ 5.000. Se quiser, mostro opções Seiko dentro desse orçamento.'
    assert check_pedido(result,contract).pass_check and check_fatos(result,contract).pass_check
    positive=result.model_copy(update={'reply_text':'Cabe no orçamento por R$ 6.099,99.'})
    assert 'presented_over_budget' in check_pedido(positive,contract).issues
    assert 'fact_price_over_budget' in check_fatos(positive,contract).issues
    ordinary=result.model_copy(deep=True)
    ordinary.response_metadata={}
    assert 'presented_over_budget' in check_pedido(ordinary,contract).issues


def test_inspection_can_explain_wrong_color_without_offering_it_as_matching():
    from app.sales.answer_council import check_pedido, check_fatos
    from app.sales.turn_contract import TurnContract
    contract = TurnContract(color='azul')
    result = AgentResult(reply_text='Esse modelo não serve para mostrador azul, porque ele é preto.',
        intent='commerce', commercial_data={'products':[{'id':'1','name':'Seiko Preto','_revalidated':True}]},
        response_metadata={'identity_inspection':True})
    assert check_pedido(result,contract).pass_check
    assert check_fatos(result,contract).pass_check
    from app.verify.double_check import run_phase0_double_check
    from app.models import IncomingMessage
    incoming=IncomingMessage(text='Quero mostrador azul. Esse modelo serve?',conversation_id='test')
    assert not any(i.code=='color_mismatch' for i in run_phase0_double_check(incoming=incoming,result=result))
    result.reply_text='Esse modelo tem mostrador azul.'
    assert 'ignored_color' in check_pedido(result,contract).issues
    assert 'fact_color_mismatch' in check_fatos(result,contract).issues
    assert any(i.code=='color_mismatch' for i in run_phase0_double_check(incoming=incoming,result=result))
