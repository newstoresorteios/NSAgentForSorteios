"""Bounded read-only regression campaign after the consistency fixes."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_launch_conversation_suite import build, MUTATIONS
from app.evaluation.regression_models import RegressionSuite


def build_suite(candidate=False):
    suite = build()
    suite.update(name='consistency-a88e8d4-five-usd', version=5, minimum_cases_per_category=1)
    budget = json.loads(suite['configuration_overrides']['evaluationCampaignPolicy'])
    budget.update(campaign_id='consistency-a88e8d4-new5-20260924', max_cost_usd=5, max_calls=65)
    suite['configuration_overrides']['evaluationCampaignPolicy'] = json.dumps(budget)
    if candidate:
        suite.update(name='consistency-root-cause-candidate', version=1)
        budget.update(campaign_id='consistency-root-cause-new5-20260924', max_cost_usd=5, max_calls=65)
        suite['configuration_overrides']['evaluationCampaignPolicy'] = json.dumps(budget)

    def step(text, requirement):
        return {'input': text, 'expected': {'requirements': [requirement],
                'forbidden_tools': MUTATIONS, 'handoff': 'forbidden', 'handoff_offer': 'forbidden'}}

    suite['scenarios'] = [{
        'key': 'screenshot_orient_2500', 'category': 'conversa', 'split': 'development',
        'critical': True, 'environment': 'live_readonly', 'steps': [
            step('Olá', 'Cumprimenta sem oferecer produto ou pagamento antigo.'),
            step('quero um relogio', 'Pergunta preferência antes de recomendar modelo específico.'),
            step('orient', 'Preserva marca Orient e pergunta o orçamento ou outra preferência útil.'),
            step('2500', 'Interpreta 2500 como orçamento de R$ 2.500, mantém Orient e continua atendimento sem mensagem genérica de insuficiência.'),
        ]}, {
        'key': 'technical_and_reset', 'category': 'conversa', 'split': 'development',
        'critical': True, 'environment': 'live_readonly', 'steps': [
            step('Quero um Citizen automático, não Eco-Drive, até R$ 3.000 no Pix.', 'Mantém Citizen automático, exclui solar/Eco-Drive e respeita orçamento.'),
            step('Não é para presente, é para mim. Preto, pulseira de borracha, vidro mineral, 200 metros, movimento 8204.', 'Preserva orçamento e marca, entende uso pessoal e separa atributos técnicos de orçamento e referência.'),
            step('NY0120-01EE. Confirme cada característica e o preço no Pix.', 'Identifica somente a referência solicitada, confirma características com evidência ou explica as lacunas.'),
            step('Reinicia a conversa', 'Confirma nova busca sem oferta ou retomada de pagamento.'),
            step('Quero um Orient clássico até R$ 2.500.', 'Não herda cor preta, pulseira de borracha, mineral ou Citizen da busca anterior.'),
        ]}]
    suite['scenarios'].extend([
        {'key': 'orient_identity_negative', 'category': 'conversa', 'split': 'development',
         'critical': True, 'environment': 'live_readonly', 'steps': [
             step('Quero o Orient M-Force Land RA-AC0N02Y10B. Confirme cor, vidro, movimento, tamanho, Pix e prazo.',
                  'Identifica a referência exata; confirma somente características verificadas, sem confundir tamanho da pulseira com caixa.'),
             step('Então é azul, mineral, até R$ 3.000 no Pix e chega amanhã?',
                  'Corrige individualmente afirmações incompatíveis com o produto consultado, sem inventar preço ou prazo e sem fallback genérico.'),
         ]},
        {'key': 'qualification_changes', 'category': 'conversa', 'split': 'development',
         'critical': True, 'environment': 'live_readonly', 'steps': [
             step('Quero uma indicação de relógio Orient clássico até R$ 3.500.',
                  'Respeita Orient, clássico e orçamento; qualifica ou apresenta opções fundamentadas.'),
             step('Não é para presente, é para mim. Automático com pulseira de aço.',
                  'Entende uso pessoal, automático e aço sem perder marca, orçamento ou estilo.'),
             step('Prefiro quartzo, não automático. Social, estilo dress. Mantenha o restante.',
                  'Substitui automático por quartzo e trata social/dress como estilo equivalente, mantendo Orient, aço e orçamento.'),
             step('Pode ser qualquer movimento e qualquer pulseira. Mostre as opções sem mais perguntas.',
                  'Remove apenas restrições de movimento e pulseira; respeita marca, orçamento e estilo, sem prolongar qualificação.'),
             {'input': 'Quero falar com uma pessoa.', 'expected': {
                 'requirements': ['Respeita pedido explícito de atendimento humano.'],
                 'forbidden_tools': MUTATIONS, 'handoff': 'required'}},
         ]},
    ])
    return RegressionSuite.model_validate(suite).model_dump(mode='json')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--candidate', action='store_true')
    args = parser.parse_args()
    args.output.write_text(json.dumps(build_suite(args.candidate), ensure_ascii=False, indent=2), encoding='utf-8')
