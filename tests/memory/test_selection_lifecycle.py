from app.commerce.commerce_context import CommerceConversationState,evolve_commerce_state
from app.models import AgentResult

def test_inspection_does_not_create_purchase_choice_but_buy_does():
    state=CommerceConversationState(checkout_draft={'address':{'zip_code':'88030300'}})
    result=AgentResult(reply_text='Modelo consultado',intent='commerce',response_metadata={
        'domain':'commerce','goal':'inspect','active_product':{'product_id':'a','name':'Produto A'}})
    mentioned=evolve_commerce_state(state,result)
    assert mentioned.active_product.product_id=='a'
    assert mentioned.purchase_target is None
    assert mentioned.selection_status=='mentioned'
    result.response_metadata['goal']='buy'
    chosen=evolve_commerce_state(mentioned,result)
    assert chosen.purchase_target.product_id=='a'
    assert chosen.selection_status=='chosen'
    assert chosen.checkout_draft.address.zip_code=='88030300'
