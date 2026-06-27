from __future__ import annotations

SITE_URL = "https://www.sorteionewstore.com.br/"
STORE_URL = "https://www.newstorerj.com.br/"

REGISTER_PHONE_MESSAGE = (
    "Não encontramos telefone cadastrado na sua conta. "
    f"Acesse {SITE_URL}, faça login e inclua seu telefone no perfil. "
    "Use o mesmo número deste WhatsApp para consultar saldo, cupom e participações com segurança."
)

THIRD_PARTY_REFUSAL = (
    "Por segurança, só consulto saldo, cupom e participações do telefone desta conversa. "
    "Não é possível consultar dados de outras pessoas pelo WhatsApp."
)

# Tabela de referência do site (cartão presente x valor mínimo de compra).
CARD_USAGE_TABLE = (
    (50_00, 250_00, 150_000),
    (251_00, 600_00, 350_000),
    (601_00, 800_00, 550_000),
    (801_00, 1_000_00, 750_000),
    (1_101_00, 2_100_00, 1_500_000),
    (2_101_00, 3_100_00, 2_250_000),
    (3_101_00, 4_200_00, 3_000_000),
)


def min_purchase_for_credit_cents(credit_cents: int) -> int | None:
    for low, high, min_purchase in CARD_USAGE_TABLE:
        if low <= credit_cents <= high:
            return min_purchase
    return None


def build_site_knowledge_text() -> str:
    return f"""
Base oficial New Store Sorteios ({SITE_URL}):

Proposta:
- Sorteio em que você concorre a prêmios e recebe 100% do valor investido de volta em Cartão Presente Digital.
- Sorteio válido até preencher a tabela, com base no resultado oficial da Lotomania (Caixa).

Como funciona:
- A vaga só é confirmada após compensação do pagamento.
- O sorteio acontece quando todos os números são vendidos.
- Ganhador: participante com o último número sorteado pela Lotomania.
- Prazo máximo: 7 dias após abertura da rodada.
- Frete do prêmio: por conta do vencedor.
- Cartão Presente não é cumulativo com prêmio nem com outras promoções.

Cartão Presente Digital:
- Saldo acumulativo em um único cartão.
- Validade de 6 meses, renovada a cada nova participação.
- Uso exclusivo no site da New Store Relógios ({STORE_URL}).
- Código pessoal e intransferível.
- Sem conversão em dinheiro.
- Não compra outro cartão-presente com crédito de sorteio.
- Pode usar parte do saldo ou em mais de um produto, respeitando a tabela.
- Desconto Pix pode exigir aplicação manual pela equipe.

FAQ resumido:
1. Sorteio baseado na Lotomania; ganhador = último número sorteado.
2. Sorteio após venda de todos os números.
3. Participação gera crédito no site + concorre ao prêmio.
4. Cartão só no site New Store, conforme tabela de utilização.
5. Crédito não transferível.
6. Frete do prêmio não incluso.
7. Resultados e novas rodadas também no grupo oficial WhatsApp.

Transparência: resultado conferível no site oficial da Caixa Econômica Federal.
""".strip()


def build_rules_reply() -> str:
    return (
        "Sorteio New Store: você concorre ao prêmio e recebe 100% do valor investido em Cartão Presente Digital "
        f"para usar em {STORE_URL}. "
        "O sorteio usa a Lotomania (último número sorteado vence). "
        "A vaga confirma após pagamento; o sorteio ocorre quando a tabela enche. "
        "Cartão: validade 6 meses (renovável), pessoal, sem dinheiro, uso exclusivo no site. "
        f"Dúvidas e novas rodadas: {SITE_URL} e grupo oficial WhatsApp."
    )


def build_simulation_reply(credit_cents: int) -> str:
    from .repository import format_cents_to_brl

    credit_label = format_cents_to_brl(credit_cents)
    min_purchase = min_purchase_for_credit_cents(credit_cents)
    if min_purchase is None:
        return (
            f"Com crédito de {credit_label}, consulte a tabela completa em {SITE_URL} "
            "ou informe um valor dentro das faixas publicadas no simulador do site."
        )

    min_label = format_cents_to_brl(min_purchase)
    return (
        f"Simulação: com {credit_label} de Cartão Presente, a compra deve ser superior a {min_label} "
        f"(referência da tabela oficial em {SITE_URL}). "
        f"Use o código no checkout de {STORE_URL}. Compras via Pix podem precisar de aplicação manual pela equipe."
    )
