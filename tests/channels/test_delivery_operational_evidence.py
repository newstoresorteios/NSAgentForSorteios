import json
from types import SimpleNamespace
from unittest.mock import Mock
import httpx
import pytest

from app.channels.delivery_errors import record_delivery_failure


def test_permanent_failure_emits_operator_signal_without_raw_provider_data(monkeypatch):
    log = Mock()
    monkeypatch.setattr('app.ops.observability.log_event', log)
    record_delivery_failure({'error': 'meta_authentication_failed', 'token': 'sensitive', 'recipient': 'private'},
                            outbox_id=1, provider='meta', channel='instagram')
    assert log.call_args.args[0] == 'delivery.operator_action_required'
    payload = log.call_args.args[1]
    assert payload['reason'] == 'channel_authentication'
    assert payload['retry_automatically'] is False
    assert 'sensitive' not in json.dumps(payload) and 'private' not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize('configured,expected', [('correct', True), ('wrong', False), ('', None)])
async def test_readonly_probe_checks_token_belongs_to_configured_account(monkeypatch, configured, expected):
    from app.channels import meta_instagram as meta
    monkeypatch.setattr(meta, 'get_settings', lambda: SimpleNamespace(
        meta_page_access_token='test-token', meta_ig_business_account_id=configured))
    seen = []
    def handler(request):
        seen.append(request.method)
        if request.url.path.endswith('/me'):
            body = {'id': 'correct', 'user_id': 'alternate', 'username': 'test'}
        elif request.url.path.endswith('/subscribed_apps'):
            body = {'data': [{'id': 'app', 'subscribed_fields': ['messages']}]}
        else:
            body = {'data': []}
        return httpx.Response(200, json=body)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    result = await meta.probe_instagram_graph_subscriptions()
    assert result['account_matches'] is expected
    assert result['messaging_ready'] is (expected is True)
    assert result['credential_valid'] is True
    assert seen == ['GET', 'GET', 'GET']
