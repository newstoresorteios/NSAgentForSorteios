"""In-memory commerce contracts. This module has no network or database client."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
import re
import unicodedata


def folded(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(value).lower()) if not unicodedata.combining(c))


class CommerceSimulator:
    def __init__(self, specification, state=None):
        self.spec = deepcopy(specification)
        self.state = deepcopy(state or {'items': [], 'orders': [], 'calls': {}, 'events': []})
        self.products = {str(p['id']): deepcopy(p) for p in self.spec.get('products', [])}

    def index_candidates(self, interpretation, limit):
        from app.catalog.retrieval.hard_filter import hard_filter_products
        return hard_filter_products(list(deepcopy(self.products).values()), interpretation,
                                    mode='recommendation', allow_unknown_features=True)[:limit]

    def cart(self):
        items = []
        for item in self.state['items']:
            product = self.products.get(item['product_id'], {})
            price = Decimal(str(product.get('current_price', product.get('price', 0))))
            items.append({**item, 'name': product.get('name'), 'price': float(price),
                          'total': float(price * item['quantity'])})
        return {'cart_id':'SIM-CART', 'session_id':'SIM-SESSION', 'cart_session_id':'SIM-SESSION',
                'cart_url':self.spec.get('cart_url', 'https://checkout.example.test/SIM-SESSION'),
                'items':items, 'total':float(sum(Decimal(str(i['total'])) for i in items))}

    def execute(self, tool, arguments):
        count = self.state['calls'].get(tool, 0) + 1
        self.state['calls'][tool] = count
        fault = (self.spec.get('faults') or {}).get(tool)
        if fault and (not fault.get('calls') or count in fault['calls']):
            return {'error':'simulated_upstream_failure', 'status_code':fault.get('status_code',503),
                    '_simulated_fault':True}
        args = deepcopy(arguments)
        pid = str(args.get('product_id', ''))
        if tool == 'get_product':
            return deepcopy(self.products.get(pid, {'error':'product_not_found','status_code':404}))
        if tool == 'search_products':
            rows = list(deepcopy(self.products).values())
            for field in ('brand', 'reference', 'ean', 'category_id'):
                if args.get(field): rows = [p for p in rows if folded(p.get(field,'')) == folded(args[field])]
            terms = args.get('tokens') or args.get('name') or args.get('query')
            if terms:
                tokens = re.findall(r'\w+', folded(' '.join(terms) if isinstance(terms,list) else terms))
                rows = [p for p in rows if all(t in folded(json.dumps(p,ensure_ascii=False)) for t in tokens)]
            price_range = re.findall(r'\d+(?:\.\d+)?', str(args.get('current_price_range','')))
            if len(price_range) == 2:
                lo, hi = map(float,price_range)
                rows = [p for p in rows if lo <= float(p.get('current_price',p.get('price',0))) <= hi]
            if args.get('available') is True:
                rows = [p for p in rows if str(p.get('available','1')).lower() not in ('0','false')]
            total=len(rows); limit=max(1,int(args.get('limit') or 20)); page=max(1,int(args.get('page') or 1))
            return {'products':rows[(page-1)*limit:page*limit], 'paging':{'total':total,'page':page,'limit':limit}}
        if tool == 'list_categories':
            categories = self.spec.get('categories') or [{'id':'1','name':'Relógios','parent_id':''}]
            return {'categories':deepcopy(categories),'paging':{'total':len(categories),'page':1,'limit':50}}
        if tool in ('get_category','get_category_tree'):
            category=next((c for c in self.spec.get('categories',[]) if str(c['id'])==str(args.get('category_id'))),{})
            return {'tree':{'category':deepcopy(category)}} if tool.endswith('tree') else deepcopy(category)
        if tool == 'list_product_variants':
            return {'variants':deepcopy(self.products.get(pid,{}).get('variants',[]))}
        if tool == 'get_product_variant':
            return next((deepcopy(v) for p in self.products.values() for v in p.get('variants',[]) if str(v['id'])==str(args.get('variant_id'))),{'error':'variant_not_found'})
        if tool == 'check_inventory':
            return {k:deepcopy(self.products.get(pid,{}).get(k)) for k in ('stock','available','available_in_store','available_for_purchase','upon_request','availability')}
        if tool == 'get_product_link':
            product=self.products.get(pid,{})
            return {'product_id':pid, 'product_name':product.get('name'),
                    'product_url':product.get('product_url') or product.get('url')}
        if tool in ('create_cart','set_cart_item_quantity'):
            if pid not in self.products: return {'error':'product_not_found','status_code':404}
            quantity=int(args.get('quantity',1))
            if quantity < 1: return {'error':'invalid_quantity','status_code':422}
            item=next((i for i in self.state['items'] if i['product_id']==pid and i.get('variant_id')==args.get('variant_id')),None)
            if item: item['quantity']=quantity if tool=='set_cart_item_quantity' else item['quantity']+quantity
            else: self.state['items'].append({'product_id':pid,'variant_id':args.get('variant_id'),'quantity':quantity})
            self.state['events'].append({'tool':tool,'arguments':args})
            return self.cart()
        if tool in ('get_cart','get_cart_complete'): return self.cart()
        if tool == 'delete_cart':
            self.state['items']=[]; self.state['events'].append({'tool':tool,'arguments':args})
            return {'success':True}
        if tool == 'get_payment_options':
            return deepcopy(self.spec.get('payment_options', {'payment_options':{}}))
        if tool in ('get_shipping','calculate_shipping','quote_shipping'):
            return deepcopy(self.spec.get('shipping', {'shipping_options':[]}))
        if tool in ('get_order','get_order_complete','get_order_payment'):
            order=next((o for o in self.spec.get('orders',[])+self.state['orders'] if str(o['id'])==str(args.get('order_id'))),None)
            return deepcopy(order or {'error':'order_not_found','status_code':404})
        if tool == 'list_orders':
            customer_id=args.get('customer_id')
            rows=[o for o in self.spec.get('orders',[])+self.state['orders']
                  if customer_id is not None and str(o.get('customer_id'))==str(customer_id)]
            return {'orders':deepcopy(rows),'paging':{'total':len(rows),'page':1,'limit':10}}
        if tool == 'create_order':
            order={'id':str(900000+len(self.state['orders'])+1),'status':'AGUARDANDO PAGAMENTO',
                   'payment_status':'pending','has_payment':False,'total':self.cart()['total'], 'items':self.cart()['items']}
            self.state['orders'].append(order); self.state['events'].append({'tool':tool,'arguments':args})
            return deepcopy(order)
        if tool == 'search_customer': return {'customers':deepcopy(self.spec.get('customers',[]))}
        if tool == 'get_customer':
            return next((deepcopy(c) for c in self.spec.get('customers',[]) if str(c['id'])==str(args.get('customer_id'))),{'error':'customer_not_found'})
        if tool in ('list_coupons','get_coupon'): return {'coupons':[]}
        return {'error':'simulation_contract_missing','tool':tool}
