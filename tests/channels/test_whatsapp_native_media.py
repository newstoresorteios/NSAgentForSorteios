import json
from io import BytesIO
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from app.models import AgentResult, BrevoSendResult, IncomingMessage
from tests.channels.test_brevo_multichannel_send import _settings


def configured(**overrides):
    return _settings(meta_whatsapp_media_enabled=True, meta_whatsapp_access_token="test-token",
        meta_whatsapp_phone_number_id="123", meta_whatsapp_graph_version="v23.0", **overrides)


def incoming():
    return IncomingMessage(provider="brevo", channel="whatsapp", sender_phone="5511999999999")


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_uploads_real_file_and_sends_media_id_not_link(monkeypatch):
    import app.channels.whatsapp_media as media
    requests = []
    data = image_bytes()
    def handler(request):
        requests.append(request)
        assert request.headers['authorization'] == 'Bearer test-token'
        assert request.extensions['timeout']['read'] == 20
        if request.method == 'GET':
            assert request.url.path == '/v23.0/123'
            assert request.url.params['fields'] == 'display_phone_number'
            return httpx.Response(200, json={'display_phone_number': '5511000000000'})
        if request.url.path.endswith('/media'):
            assert request.method == 'POST'
            assert request.headers['content-type'].startswith('multipart/form-data;')
            assert data in request.content
            assert b'name="messaging_product"\r\n\r\nwhatsapp' in request.content
            assert b'name="type"\r\n\r\nimage/png' in request.content
            assert b'filename="produto-1.png"' in request.content
            return httpx.Response(200, json={'id': 'uploaded-file'})
        assert request.url.path == '/v23.0/123/messages'
        body = json.loads(request.content)
        assert body == {'messaging_product': 'whatsapp', 'recipient_type': 'individual',
                        'to': '5511999999999', 'type': 'image', 'image': {'id': 'uploaded-file'}}
        return httpx.Response(200, json={'messages': [{'id': 'wamid.test'}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(media, 'get_settings', configured)
    monkeypatch.setattr(media.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    download = AsyncMock(return_value=(data, 'image/png'))
    monkeypatch.setattr(media, 'download_trusted_media', download)
    result = await media.send_image_files(incoming(), ['https://images.tcdn.com.br/p.png'])
    assert result.ok and not result.dry_run
    assert result.provider_response['images_accepted'] == 1
    assert len(requests) == 3
    assert download.await_args.kwargs['max_bytes'] == 5_000_000
    assert download.await_args.kwargs['allowed_suffixes'] == ('tcdn.com.br',)


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario,error', [
    ('sender', 'whatsapp_media_sender_mismatch'),
    ('invalid_file', 'whatsapp_media_upload_failed'),
    ('upload', 'whatsapp_media_rejected'),
    ('send', 'whatsapp_media_rejected'),
    ('missing_id', 'whatsapp_media_delivery_uncertain'),
    ('timeout', 'whatsapp_media_delivery_uncertain'),
    ('partial', 'whatsapp_media_partial_delivery'),
])
async def test_failures_never_report_success_or_expose_provider_secrets(monkeypatch, scenario, error):
    import app.channels.whatsapp_media as media
    posts = []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'display_phone_number': '999' if scenario == 'sender' else '5511000000000'})
        posts.append(request.url.path)
        if request.url.path.endswith('/media'):
            return httpx.Response(403, json={'secret': 'private'}) if scenario == 'upload' else httpx.Response(200, json={'id': 'file'})
        if scenario == 'timeout':
            raise httpx.ReadTimeout('private token', request=request)
        if scenario == 'send' or (scenario == 'partial' and posts.count('/v23.0/123/messages') == 2):
            return httpx.Response(400, json={'secret': 'private'})
        return httpx.Response(200, json={} if scenario == 'missing_id' else {'messages': [{'id': 'wamid.test'}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(media, 'get_settings', configured)
    monkeypatch.setattr(media.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(media, 'download_trusted_media', AsyncMock(return_value=(
        b'invalid' if scenario == 'invalid_file' else image_bytes(), 'image/png')))
    result = await media.send_image_files(incoming(), ['https://images.tcdn.com.br/1.png', 'https://images.tcdn.com.br/2.png'])
    assert not result.ok and result.error == error
    assert 'private' not in result.model_dump_json()
    if scenario in {'sender', 'invalid_file', 'upload'}:
        assert '/v23.0/123/messages' not in posts
    if scenario == 'partial':
        assert result.provider_response['images_accepted'] == 1
    from app.channels.delivery_errors import permanent_delivery_failure
    if scenario in {'timeout', 'partial', 'missing_id'}:
        assert permanent_delivery_failure({'error': result.error})


@pytest.mark.asyncio
async def test_missing_configuration_does_not_send_text_in_place_of_file(monkeypatch):
    import app.channels.brevo_client as brevo
    monkeypatch.setattr(brevo, 'get_settings', _settings)
    text = AsyncMock()
    monkeypatch.setattr(brevo, '_send_whatsapp_transactional_reply', text)
    result = AgentResult(reply_text='Segue a foto.', response_metadata={'outbound_image_url': 'https://images.tcdn.com.br/p.jpg'})
    sent = await brevo.send_brevo_reply(incoming(), result)
    assert not sent.ok and sent.error == 'whatsapp_media_not_configured'
    assert result.response_metadata['fallback_link_sent'] is False
    assert result.response_metadata['native_media_sent'] is False
    text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('media_ok', [True, False])
async def test_files_are_sent_before_full_text_and_never_replaced_with_links(monkeypatch, media_ok):
    import app.channels.brevo_client as brevo
    import app.channels.whatsapp_media as media
    monkeypatch.setattr(brevo, 'get_settings', configured)
    urls = [f'https://images.tcdn.com.br/{i}.jpg' for i in range(2)]
    events = []
    async def send_files(_incoming, received):
        events.append('files')
        assert received == urls
        return BrevoSendResult(ok=media_ok, dry_run=False,
            error=None if media_ok else 'whatsapp_media_rejected',
            provider_response={'route': 'whatsapp_cloud_media', 'images_accepted': 2 if media_ok else 0})
    async def send_text(_incoming, text):
        events.append('text')
        assert text == full_text
        return BrevoSendResult(ok=True, dry_run=False)
    monkeypatch.setattr(media, 'send_image_files', send_files)
    monkeypatch.setattr(brevo, '_send_whatsapp_transactional_reply', send_text)
    full_text = 'Descrição completa. ' * 90 + 'https://loja.test/produto'
    result = AgentResult(reply_text=full_text + '\n' + '\n'.join(urls), response_metadata={'outbound_image_urls': urls})
    sent = await brevo.send_brevo_reply(incoming(), result)
    assert sent.ok is media_ok
    assert events == (['files', 'text'] if media_ok else ['files'])
    assert result.reply_text == full_text
    assert result.response_metadata['native_media_sent'] is media_ok
    assert result.response_metadata['fallback_link_sent'] is False


@pytest.mark.asyncio
async def test_transactional_text_preserves_every_character_and_final_link(monkeypatch):
    import app.channels.brevo_client as brevo
    full_text = 'á🙂 ' * 500 + '\nhttps://loja.test/link-final'
    def handler(request):
        assert json.loads(request.content)['text'] == full_text
        assert 'imageUrl' not in json.loads(request.content)
        return httpx.Response(201, json={'messageId': 'text-id'})
    original = httpx.AsyncClient
    monkeypatch.setattr(brevo, 'get_settings', _settings)
    monkeypatch.setattr(brevo.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert (await brevo._send_whatsapp_transactional_reply(incoming(), full_text)).ok
