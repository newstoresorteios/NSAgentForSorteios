"""Seed read-only production-history cases with independent outcome criteria."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.evaluation.repository import cases_from_rows
from app.evaluation.regression_models import RegressionSuite

ORACLES={
 7:'Responde à busca pelo mostrador preto a partir do catálogo, sem confundir cor da caixa/pulseira com mostrador.',
 16:'Pesquisa a referência Luminox solicitada e distingue correspondência exata, alternativa e ausência no catálogo.',
 77:'Busca relógios femininos até R$ 3.000 e apresenta opção identificável que atenda; se não houver, explica a busca sem inventar.',
 82:'Pesquisa PRX automático de 35 mm com madre-pérola; não substitui silenciosamente por quartzo, outro tamanho ou mostrador.',
 191:'Encaminha avaliação do relógio do cliente para consultor sem inventar valor de compra ou permuta.',
 222:'Responde sobre o Pix do produto do contexto, se identificado; caso contrário pede o modelo. Usa preço e política publicados.',
 242:'Responde sobre prazo para Porto Alegre com informação confirmada ou orientação de consulta no site, sem exigir mudança de canal.',
 446:'Resolve o primeiro produto da lista anterior e consulta seu valor atual; não inventa preço nem pede toda a seleção novamente.',
 455:'Aplica o novo orçamento máximo de R$ 6.000 ao pedido em andamento sem reiniciar perguntas já respondidas.',
 458:'Retoma a consulta que o cliente cobrou e traz resultado concreto ou limitação específica da consulta, não apenas promessa de pesquisar.',
 473:'Pesquisa Seiko SPB515; identifica o modelo correto ou informa explicitamente a ausência confirmada, sem trocar por outro SKU como se fosse igual.',
 631:'Busca relógios dourados abaixo de R$ 3.500; não apresenta pretos/prateados como correspondências confirmadas.',
 632:'Reconhece a correção de que as opções não eram douradas e refaz o filtro de cor sem repetir a mesma lista inadequada.',
 641:'Pesquisa Baltic MK2 e distingue a linha/modelo exatos de alternativas.',
 663:'Respeita teto de R$ 2.500 e caixa acima de 40 mm, sem acrescentar restrição de marca não pedida.',
 664:'Reconhece que Tag não foi pedida e abandona essa restrição ao continuar a consulta.',
 672:'Corrige a interpretação de marca e mantém os critérios efetivamente pedidos pelo cliente.',
 716:'Resolve o primeiro produto já apresentado e usa fotos ou link de mídia oficiais, ou explica limitação concreta sem inventar imagem.',
 726:'Pesquisa Seiko até R$ 3.000 e responde com resultado identificável dentro do teto ou ausência fundamentada.',
 744:'Troca para Orient Open Heart preto, sem manter outra linha do histórico ou afirmar disponibilidade não confirmada.',
 770:'Aplica o teto atualizado de R$ 10.000 aos critérios em andamento e avança de forma útil.',
 778:'Responde se o primeiro produto é automático usando a ficha daquele SKU; não inicia compra por causa do número 1.',
 783:'Reconhece o desencontro e retoma o pedido conhecido sem reiniciar uma saudação ou inventar intenção de compra.',
 787:'Consulta relógios automáticos com safira até R$ 2.500; distingue preço confirmado, sob consulta e ausência de correspondência, sem handoff genérico quando houver dados úteis.',
}


def build(rows,workspace):
    cases={c['source_response_id']:c for c in cases_from_rows(rows,workspace,limit=2000,history_turns=10)}
    scenarios=[]
    for ident,oracle in ORACLES.items():
        c=cases[ident]
        scenarios.append({'key':f'history_{ident}','category':'historico_real','split':'development',
            'source_response_ids':[ident],'channel':c['channel'],'history':c['history'],
            'initial_state':c['initial_state'],'recorded_at':c['recorded_at'],
            'environment':'live_readonly','steps':[{'input':c['input'],'expected':{
                'requirements':[oracle],
                'forbidden_claims':['Pedido, pagamento, reserva ou envio confirmado sem evidência da operação.'],
                'forbidden_tools':['create_order','create_cart','set_cart_item_quantity','cancel_order']}}]})
    return RegressionSuite(name='historico-real-regressao-2026-09',version=1,categories=['historico_real'],
        configuration_overrides={'agent_max_llm_calls_per_turn':5,'agent_max_llm_calls_per_turn_complex':6,
                                 'historyEvaluationModel':'gpt-5.4'},scenarios=scenarios)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--workspace',required=True)
    a=p.parse_args();r=build(json.loads(a.source.read_text(encoding='utf-8')),a.workspace)
    a.output.write_text(r.model_dump_json(indent=2),encoding='utf-8')
    print(json.dumps({'cases':len(r.scenarios),'turns_of_prior_context':sum(len(s.history) for s in r.scenarios)}))
