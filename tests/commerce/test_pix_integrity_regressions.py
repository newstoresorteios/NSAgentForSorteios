"""Independent negative cases for PIX identity, reconciliation and recovery."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.commerce import pix_payment_repository as repo
from app.commerce import pix_payment_service as service
from app.commerce import pix_settlement as settlement
from app.commerce.mercadopago_client import PixPaymentCreated
from app.tray import tray_webhook_consumer as consumer
from app.tray.tray_adapter_client import TrayAdapterError


def test_same_price_reviews_have_different_keys_and_retries_keep_key():
    created = PixPaymentCreated('fake-payment', 'pending', 'fake-qr', '', 'fake-qr', 1800, 100.0, None, {})
    provider = AsyncMock(return_value=created)
    async def run():
        with patch.object(service, 'create_pix_payment', provider), patch.object(repo, 'upsert_pix_payment_created', return_value=1):
            for sku, amount in [('SKU-A', 100), ('SKU-A', 100.0), ('SKU-B', 100)]:
                await service.create_and_persist_pix_payment(transaction_amount=amount, description=sku,
                    payer_email='fake@example.invalid', cart_session_id='fake-cart', external_reference='fake-cart',
                    checkout_snapshot={'order_review_version':sku, 'order_payload':{'products':[{'product_id':sku}]}},
                    settings=SimpleNamespace(pix_exp_min=30))
    asyncio.run(run())
    keys = [call.kwargs['idempotency_key'] for call in provider.call_args_list]
    assert keys[0] == keys[1] and keys[1] != keys[2]


def _row(**overrides):
    result = {'mp_payment_id':'fake-payment', 'status':'approved', 'amount_cents':10000,
        'settlement_status':'pending', 'tray_order_id':None, 'currency':'BRL',
        'checkout_snapshot':{'expected_amount_cents':10000,
            'order_payload':{'session_id':'fake-cart','products':[{'product_id':'A','price':'100','quantity':1}]}}}
    result.update(overrides)
    return result


@pytest.mark.parametrize('overrides,total,mp_change,expected', [
    ({'settlement_status':'failed','settlement_error':'amount_mismatch'}, '100', {}, 'pix_settlement_rejected'),
    ({}, '999.99', {}, 'tray_order_amount_mismatch'),
    ({}, '100', {'transaction_amount':99}, 'amount_mismatch'),
    ({}, '100', {'currency_id':'USD'}, 'currency_mismatch'),
    ({}, '100', {'status':'refunded'}, 'pix_not_approved'),
    ({}, '100', {'id':'other-payment'}, 'mp_payment_id_mismatch'),
])
def test_tray_event_does_not_bypass_financial_gate(overrides,total,mp_change,expected):
    row = _row(**overrides)
    client = SimpleNamespace(get_order=AsyncMock(return_value={'order':{'session_id':'fake-cart','total':total}}))
    payment = {'id':'fake-payment','status':'approved','transaction_amount':100,'currency_id':'BRL',**mp_change}
    with patch.object(repo,'get_pix_payment_by_tray_order_id',return_value=None), \
         patch.object(repo,'get_pix_payment_by_cart_session_id',return_value=row), \
         patch.object(repo,'mark_pix_settlement') as writer, \
         patch.object(settlement,'get_payment',AsyncMock(return_value=payment)):
        result = asyncio.run(consumer.confirm_pix_for_tray_order('fake-order',client=client))
    assert result['ok'] is False and result['reason'] == expected
    writer.assert_not_called()


def test_valid_tray_event_reconciles_after_fresh_verification():
    client = SimpleNamespace(get_order=AsyncMock(return_value={'order':{'session_id':'fake-cart','total':'100'}}))
    with patch.object(repo,'get_pix_payment_by_tray_order_id',return_value=None), \
         patch.object(repo,'get_pix_payment_by_cart_session_id',return_value=_row()), \
         patch.object(repo,'mark_pix_settlement',return_value={'settlement_status':'completed'}) as writer, \
         patch.object(settlement,'get_payment',AsyncMock(return_value={'id':'fake-payment','status':'approved','transaction_amount':100,'currency_id':'BRL'})) as fetch:
        result = asyncio.run(consumer.confirm_pix_for_tray_order('fake-order',client=client))
    assert result['ok'] is True
    fetch.assert_awaited_once_with('fake-payment')
    assert writer.call_args.kwargs['tray_order_id'] == 'fake-order'


@pytest.mark.parametrize('orders', [[None], ['invalid-order'], [{'id':'other'}], [{'id':'other','session_id':'other'}],
    [{'id':'1','session_id':'fake-cart','total':'99'}],
    [{'id':'1','session_id':'fake-cart','total':'100'},{'id':'2','session_id':'fake-cart','total':'100'}]])
def test_reconciliation_requires_unique_verified_session_and_total(orders):
    client = SimpleNamespace(list_orders=AsyncMock(return_value={'orders':orders}))
    with patch.object(settlement,'TrayAdapterClient',return_value=client), pytest.raises(TrayAdapterError):
        asyncio.run(settlement.find_existing_tray_order(_row()['checkout_snapshot']['order_payload']))


def test_lookup_failure_never_creates_order():
    creator = AsyncMock()
    with patch.object(repo,'get_pix_payment_by_mp_id',return_value=_row()), \
         patch.object(repo,'claim_pix_settlement',return_value=_row(settlement_status='processing')), \
         patch.object(repo,'mark_pix_settlement'), \
         patch.object(settlement,'find_existing_tray_order',AsyncMock(side_effect=TimeoutError)):
        result = asyncio.run(settlement.settle_approved_pix_payment('fake-payment',
            mp_payload={'status':'approved','transaction_amount':100}, create_order=creator))
    assert result['reason'] == 'tray_reconciliation_failed'
    creator.assert_not_awaited()


def _connection(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    @contextmanager
    def connection():
        yield conn
    return connection


def test_repository_filters_eligibility_before_limit_and_atomically_checks_lease():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = [_row()]
    with patch.object(repo,'get_conn',_connection(cursor)):
        assert repo.list_retryable_pix_settlements(limit=10) == [_row()]
        select = cursor.execute.call_args.args[0]
        assert select.index('settlement_error') < select.index('LIMIT')
        assert "updated_at <= %s" in select
        repo.requeue_pix_settlement('fake-payment')
        update = cursor.execute.call_args.args[0]
        assert 'updated_at <= %s' in update and 'settlement_error' in update


def test_payment_snapshot_conflict_is_rejected_without_returning_other_intent():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = None  # ON CONFLICT ... WHERE did not match
    with patch.object(repo,'get_conn',_connection(cursor)), pytest.raises(ValueError,match='pix_payment_identity_conflict'):
        repo.upsert_pix_payment_created(mp_payment_id='fake-payment',status='pending',amount_cents=10000)
    sql = cursor.execute.call_args.args[0]
    assert 'checkout_snapshot = EXCLUDED.checkout_snapshot' in sql
    assert 'checkout_snapshot = public.ai_pix_payments.checkout_snapshot' in sql
