"""Bounded launch conversation checks; real catalog, no commercial mutations."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.evaluation.regression_models import RegressionSuite

MUTATIONS = ['create_cart', 'set_cart_item_quantity', 'delete_cart', 'create_order', 'cancel_order']


def build():
    def step(text, requirements, **expected):
        return {'input': text, 'expected': {'requirements': requirements,
                'forbidden_tools': MUTATIONS, **expected}}

    def scenario(key, steps):
        return {'key': key, 'category': 'conversa', 'split': 'development',
                'critical': True, 'environment': 'live_readonly', 'steps': steps}

    return RegressionSuite.model_validate({
        'name': 'launch-conversation-readonly', 'version': 4,
        'categories': ['conversa'], 'minimum_cases_per_category': 3,
        'configuration_overrides': {
            'modelRolePolicies': json.dumps({role: {'max_output_tokens': 6000}
                                            for role in ['interpretation', 'composition', 'review', 'evaluation']}),
            'evaluationCampaignPolicy': json.dumps({
            'enabled': True, 'campaign_id': 'launch-20260923-confirmation',
            'price_version': 'openai-pricing-2026-09-23',
            'max_calls': 50, 'max_tokens': 6000000, 'max_cost_usd': 4,
            'max_input_tokens_per_call': 300000,
            'prices': {'gpt-5.4-mini': {'input': 0.75, 'output': 4.50}},
        })},
        'scenarios': [
            scenario('qualificacao_ate_link', [
                step('Olá, quero um relógio.', ['Pergunta uma preferência antes de oferecer produto específico.'],
                     forbidden_claims=['Recomenda produto específico sem qualificar.']),
                step('Não tenho nenhum modelo em mente.', ['Avança a qualificação sem repetir a pergunta sobre modelo.']),
                step('Até R$ 2.500.', ['Respeita o orçamento e pergunta sobre uso ou preferência antes de escolher produto: somente orçamento não encerra a qualificação iniciada.'],
                     forbidden_claims=['Recomenda um modelo antes de conhecer uso ou preferência.'], max_price=2500),
                step('É para mim, para usar no dia a dia. Prefiro algo discreto.', ['Considera uso diário e estilo discreto, sem esquecer o orçamento.'], max_price=2500),
                step('Prefiro caixa pequena, mostrador preto e pulseira de aço.', ['Preserva tamanho, cor, pulseira e orçamento; não oferece acessório como relógio.'], max_price=2500),
                step('Pode mostrar uma opção que atenda.', ['Apresenta opção comprovadamente compatível ou explica qual critério não conseguiu confirmar.'], max_price=2500),
                step('Me mande o link do primeiro que você encontrou.', ['Mantém o produto apresentado; se nenhum foi encontrado, esclarece sem inventar um primeiro produto.']),
            ]),
            scenario('automatico_safira', [
                step('Quero um relógio automático com vidro de safira, até R$ 2.500.', ['Exige automático, safira e preço até 2500; não inclui acessórios nem produto sob consulta como preço confirmado.'], max_price=2500),
                step('Não abra mão da safira nem do automático. Pode ir até R$ 4.000.', ['Amplia somente orçamento; mantém automático e safira.'], max_price=4000),
                step('Pode mandar o link de uma opção confirmada.', ['Link apenas de produto confirmado ou limitações concretas; sem inventar disponibilidade.'], max_price=4000),
            ]),
            scenario('nao_sei_e_humano', [
                step('Quero dar um relógio de presente, mas não entendo nada.', ['Faz uma pergunta simples para orientar o presente, sem presumir modelo.']),
                step('Não sei, me ajuda a escolher.', ['Avança sem repetir a mesma pergunta; explica escolhas de maneira simples.']),
                step('Qual a diferença entre dress e diver?', ['Explica relógio social e de mergulho em linguagem simples, sem afirmar resistência de modelo não consultado.']),
                step('Quero falar com uma pessoa.', ['Respeita pedido explícito de atendimento humano.'], handoff='required'),
            ]),
        ],
    }).model_dump(mode='json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build(), ensure_ascii=False, indent=2), encoding='utf-8')
    print('3 scenarios / 14 turns; live read-only catalog; no messages to customers')
