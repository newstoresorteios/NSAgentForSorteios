"""Read each comparison target independently; never search a concatenated SKU."""
from __future__ import annotations

import re
from app.catalog.retrieval.text import fold_text
from app.configuration.runtime import policy
from app.models import ProductSubject, ProductPreferences


def comparison_references(text):
    tokens=re.findall(r'[A-Za-z0-9][A-Za-z0-9.-]*',str(text or ''))
    return list(dict.fromkeys(token for token in tokens if len(token)>=4
        and any(c.isdigit() for c in token) and any(c.isalpha() for c in token)))


async def try_product_comparison(message,interpretation,state,recent_turns):
    if interpretation is None:
        return None
    requested=interpretation.goal=='compare' or re.search(policy('comparisonQuestionPattern'),fold_text(message.text))
    if not requested:
        return None
    import app.sales_agent as sales
    references=comparison_references(message.text)
    products=[]
    if len(references)>=2:
        for reference in references[:4]:
            found=await sales.execute_tool('search_products',{'reference':reference,'limit':4})
            candidates=found.get('products') or []
            matches=[p for p in candidates if str(p.get('reference') or '').casefold()==reference.casefold()]
            if len(matches)!=1:
                continue
            detail=await sales.execute_tool('get_product',{'product_id':str(matches[0]['id'])})
            if not detail.get('error'):
                products.append({**matches[0],**detail})
    elif interpretation.references_previous_context and len(state.last_presented_products)>=2:
        for reference in state.last_presented_products[:4]:
            detail=await sales.execute_tool('get_product',{'product_id':reference.product_id})
            if not detail.get('error'):
                products.append(detail)
    else:
        return None
    if not products:
        return None
    from app.commerce.commerce_router import _product_result
    from app.sales.result_utils import mark_sales_result
    clean=interpretation.model_copy(deep=True)
    clean.subject=ProductSubject(product_type=interpretation.subject.product_type)
    clean.preferences=ProductPreferences()
    clean.goal='compare'
    clean.needs_clarification=False
    clean.answer_strategy='answer_directly'
    clean.purchase_action=clean.payment_action=clean.checkout_action=None
    clean.reference_type=None
    clean.reference_position=None
    facts=_product_result('product_search',products)
    facts.commercial_data['comparison']={'requested_references':references,'confirmed_count':len(products)}
    facts.response_metadata.update(presented_products=True,preserve_catalog_context=True,
                                   active_preferences={'comparison_product_ids':[str(p['id']) for p in products]})
    reply=await sales._sales_response_with_openai(message,{'intent':'product_comparison','goal':'compare'},
                                                facts,clean,state=state,recent_turns=recent_turns)
    return mark_sales_result(reply or facts,interpretation=clean,goal='compare',
        response_source='openai' if reply else 'deterministic_fallback',
        used_openai_responder=reply is not None,used_tray=True)
