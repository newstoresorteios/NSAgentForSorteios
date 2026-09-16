"""Private replay of a contact's model, immediate-stock and delivery requests."""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.evaluation.regression_models import RegressionSuite
from app.evaluation.repository import cases_from_rows
from build_regression_suite import product

ORACLES = {
    640: 'Consulta Baltic com pronta entrega. Só confirma pronta entrega quando a ficha ou categoria oficial sustenta; distingue peças sob encomenda.',
    641: 'Consulta Baltic MK2 preservando a exigência anterior de pronta entrega; não troca por outra linha como correspondência exata.',
    642: 'Mantém Baltic MK2 e aplica cinza e caixa de 37 mm. Se não confirmar essa combinação, explica a limitação; não substitui silenciosamente por Hermétique Tourer de outra cor.',
    643: 'Pesquisa Baltic MK2 cinza de 37 mm. Distingue correspondência exata, alternativas identificadas e ausência, sem trocar silenciosamente a linha.',
    752: 'Busca Longines Heritage preto, ou solicita apenas informação indispensável. Não bloqueia a consulta para perguntar o nome do cliente.',
    754: 'Reconhece a apresentação do nome e mantém o pedido Longines Heritage preto já informado; não pede novamente a marca ou modelo.',
    755: 'Aplica o teto de R$ 20.000 a Longines Heritage preto; não trata Hydroconquest como Heritage nem reinicia a descoberta.',
    808: 'Consulta Certina com pronta entrega. Se não confirmar essa disponibilidade, explica a limitação sem afirmar que peças sob encomenda atendem à pronta entrega.',
    809: 'Responde sobre pronta entrega versus encomenda usando os produtos já apresentados. Se a referência for ambígua, pede uma seleção específica ou distingue os itens, sem usar a pergunta como nome de modelo.',
    810: 'Informa prazo baseado em evidência do produto e na política publicada, ou explica que precisa do produto/CEP para confirmar o prazo final. Não inventa data garantida nem faz handoff genérico quando há informação útil.',
}
MUTATIONS = ['create_cart','set_cart_item_quantity','create_order','cancel_order']


def build(rows,workspace,baseline):
    cases={c['source_response_id']:c for c in cases_from_rows(rows,workspace,limit=2000,history_turns=20)}
    scenarios=[]
    def step(text,oracle):
        return {'input':text,'expected':{'requirements':[oracle], 'forbidden_tools':MUTATIONS,
            'forbidden_claims':['Pedido, pagamento, reserva ou prazo garantido sem evidência.']}}
    for ident,oracle in ORACLES.items():
        c=cases[ident]
        scenarios.append({'key':f'contact_0859_replay_{ident}','category':'contato_replay','split':'development',
            'source_response_ids':[ident],'channel':c['channel'],'history':c['history'],
            'initial_state':c['initial_state'],'recorded_at':c['recorded_at'],'environment':'live_readonly',
            'steps':[step(c['input'],oracle)]})
    for key,ids in [('baltic',[640,641,642,643]),('longines',[752,754,755]),('certina',[808,809,810])]:
        c=cases[ids[0]]
        scenarios.append({'key':f'contact_0859_sequence_{key}','category':'contato_sequencia','split':'development',
            'source_response_ids':ids,'channel':c['channel'],'history':c['history'],'initial_state':c['initial_state'],
            'recorded_at':c['recorded_at'],'environment':'live_readonly',
            'steps':[step(cases[i]['input'],ORACLES[i]) for i in ids]})
    fixture=deepcopy(baseline['scenarios'][0]['simulation'])
    fixture['products']=[
        product(92001,'Relógio Certina DS Action Azul C032.430.11.041.00','Certina',7699.99,'Automático','Safira',40.5,'Azul'),
        product(92002,'Relógio Certina DS Action Cinza C032.430.11.081.01','Certina',7699.99,'Automático','Safira',41,'Cinza',
                related_categories=['403'],order_days_availability=0,availability='Pronta entrega'),
        product(92003,'Relógio Certina DS-2 Chrono Azul C024.462.18.041.00','Certina',20299.99,'Automático','Safira',43,'Azul'),
        product(92004,'Relógio Baltic Aquascaphe MK2 Cinza BALTICMK237','Baltic',7999.99,'Automático','Safira',37,'Cinza',
                related_categories=['403'],order_days_availability=0,availability='Pronta entrega'),
        product(92005,'Relógio Baltic Hermétique Tourer Azul BALTICT37','Baltic',7699.99,'Automático','Safira',37,'Azul'),
        product(92006,'Relógio Longines Heritage Preto LHERITAGE01','Longines',15999.99,'Automático','Safira',40,'Preto'),
        product(92007,'Relógio Longines Hydroconquest Preto LHYDRO01','Longines',12699.99,'Automático','Safira',41,'Preto'),
    ]
    variations=[
        ('certina_ready',[('Quero um Certina a pronta entrega','Apresenta Certina cinza 92002 como pronta entrega; não lista 92001 ou 92003 como pronta entrega.'),
            ('Qual o prazo de entrega dele?','Mantém 92002 e distingue despacho/pronta entrega de prazo final de transporte; não inventa data.')]),
        ('baltic_refine',[('Procuro um Baltic MK2','Localiza Baltic MK2 92004, sem substituir por Hermétique Tourer.'),
            ('O cinza, de 37 mm','Mantém MK2 cinza de 37 mm, identificando 92004.')]),
        ('longines_budget',[('Quero um Longines Heritage preto','Localiza Heritage preto 92006.'),
            ('Meu teto é 20 mil reais','Mantém Heritage preto e o teto; não troca para Hydroconquest só por estar no orçamento.')]),
        ('ask_ready_or_order',[('Me mostre o Certina C032.430.11.041.00','Localiza 92001, sem afirmar pronta entrega.'),
            ('Esse modelo é pronta entrega ou sob encomenda?','Usa o prazo da ficha de 92001 e explica que não confirmou pronta entrega; não faz uma nova busca pelo texto inteiro da pergunta.')]),
    ]
    for key,turns in variations:
        scenarios.append({'key':'contact_0859_'+key,'category':'contato_variacoes','split':'development',
            'environment':'simulated_commerce','simulation':deepcopy(fixture),
            'steps':[step(text,oracle) for text,oracle in turns]})
    return RegressionSuite(name='contato-0859-regressao',version=1,
        categories=['contato_replay','contato_sequencia','contato_variacoes'],
        configuration_overrides=baseline['configuration_overrides'],scenarios=scenarios)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['source','baseline','output']:p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--workspace',required=True);a=p.parse_args()
    suite=build(json.loads(a.source.read_text(encoding='utf-8')),a.workspace,json.loads(a.baseline.read_text(encoding='utf-8')))
    a.output.write_text(suite.model_dump_json(indent=2),encoding='utf-8')
    print(json.dumps({'scenarios':len(suite.scenarios),'turns':sum(len(s.steps) for s in suite.scenarios)}))
