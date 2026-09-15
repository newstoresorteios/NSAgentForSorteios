from __future__ import annotations

import json
from app.configuration.runtime import policy

from app.configuration.runtime import message as operator_message

def SITE_URL():
    return policy('business.site_url')
def STORE_URL():
    return policy('business.store_url')
def STORE_PRONTA_ENTREGA_URL():
    return policy('business.store_pronta_entrega_url')
def NS_SALES_WHATSAPP():
    return policy('business.ns_sales_whatsapp')

def HUMAN_SUPPORT_MESSAGE():
    return operator_message('business.human_support_message.d86b19641e', NS_SALES_WHATSAPP=f'{NS_SALES_WHATSAPP()}')

def HUMAN_HANDOFF_ACK_MESSAGE():
    return operator_message('handoff_requested')

def TRADE_IN_HANDOFF_MESSAGE():
    return operator_message('trade_in_handoff' if policy('acceptsTradeIn') else 'trade_in_unavailable')

def REGISTER_PHONE_MESSAGE():
    return operator_message('business.register_phone_message.af8a374a34', SITE_URL=f'{SITE_URL()}')

def THIRD_PARTY_REFUSAL():
    return operator_message('business.third_party_refusal.7ed15f4249')

# Tabela de referência do site (cartão presente x valor mínimo de compra), em centavos.
def CARD_USAGE_TABLE():
    return tuple(tuple(band) for band in json.loads(policy('business.credit_bands')))


def min_purchase_for_credit_cents(credit_cents: int) -> int | None:
    for low, high, min_purchase in CARD_USAGE_TABLE():
        if low <= credit_cents <= high:
            return min_purchase
    return None


def credit_band_for_amount(credit_cents: int) -> tuple[int, int, int] | None:
    for band in CARD_USAGE_TABLE():
        low, high, min_purchase = band
        if low <= credit_cents <= high:
            return band
    return None


def max_applicable_credit_for_product_cents(product_cents: int) -> int:
    """Máximo de cartão aplicável dado o valor do produto (tabela oficial)."""
    if product_cents <= 0:
        return 0

    max_credit = 0
    for low, high, min_purchase in CARD_USAGE_TABLE():
        if product_cents > min_purchase:
            max_credit = max(max_credit, high)
    return max_credit


def format_card_usage_table_text() -> str:
    from app.identity.repository import format_cents_to_brl

    lines = [operator_message('business.format_card_usage_table_text.c44b529ef6')]
    for low, high, min_purchase in CARD_USAGE_TABLE():
        lines.append(
            operator_message('business.format_card_usage_table_text.59ffac20e3', value_1=f'{format_cents_to_brl(low)}', value_2=f'{format_cents_to_brl(high)}', value_3=f'{format_cents_to_brl(min_purchase)}')
        )
    lines.append(
        operator_message('business.format_card_usage_table_text.0874ed5905')
    )
    return "\n".join(lines)


def build_site_knowledge_text() -> str:
    return operator_message('business.build_site_knowledge_text.9d5fa80f06', SITE_URL=f'{SITE_URL()}', STORE_URL=f'{STORE_URL()}', value_3=f'{format_card_usage_table_text()}', NS_SALES_WHATSAPP=f'{NS_SALES_WHATSAPP()}', TRADE_IN_HANDOFF_MESSAGE=f'{TRADE_IN_HANDOFF_MESSAGE()}').strip()


def build_rules_reply() -> str:
    return (
        operator_message('business.build_rules_reply.3ca4ed85ac', STORE_URL=f'{STORE_URL()}', SITE_URL=f'{SITE_URL()}', NS_SALES_WHATSAPP=f'{NS_SALES_WHATSAPP()}')
    )


def build_simulation_reply(credit_cents: int, product_cents: int | None = None) -> str:
    from app.ops.simulation import build_purchase_simulation_reply

    return build_purchase_simulation_reply(credit_cents, product_cents=product_cents)
