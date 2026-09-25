import pytest

from app.channels.meta_instagram import (
    handle_meta_verify_challenge,
    instagram_event_skip_reason,
    messaging_event_shapes,
    parse_meta_instagram_messaging,
    payload_skeleton,
    verify_meta_signature,
)


def test_meta_signature_roundtrip():
    body = b'{"object":"instagram"}'
    secret = "test-app-secret"
    import hashlib
    import hmac

    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_meta_signature(
        app_secret=secret, body=body, signature_header=sig
    )
    assert not verify_meta_signature(
        app_secret=secret, body=body, signature_header="sha256=deadbeef"
    )
    upper = "SHA256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest().upper()
    assert verify_meta_signature(
        app_secret=secret, body=body, signature_header=upper
    )


def test_meta_signature_accepts_instagram_secret():
    from app.channels.meta_instagram import verify_meta_signatures
    import hashlib
    import hmac

    body = b'{"object":"instagram"}'
    ig_secret = "instagram-app-secret"
    sig = "sha256=" + hmac.new(ig_secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_meta_signatures(
        app_secrets=["facebook-app-secret", ig_secret],
        body=body,
        signature_header_sha256=sig,
    )
    assert not verify_meta_signatures(
        app_secrets=["facebook-app-secret"],
        body=body,
        signature_header_sha256=sig,
    )


def test_meta_verify_challenge():
    assert (
        handle_meta_verify_challenge(
            mode="subscribe",
            verify_token="tok",
            challenge="12345",
            expected_token="tok",
        )
        == "12345"
    )
    assert (
        handle_meta_verify_challenge(
            mode="subscribe",
            verify_token="wrong",
            challenge="12345",
            expected_token="tok",
        )
        is None
    )


def test_parse_meta_changes_field_messages():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-biz",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "sender": {"id": "user-2"},
                            "recipient": {"id": "ig-biz"},
                            "message": {"mid": "m2", "text": "teste"},
                        },
                    }
                ],
            }
        ],
    }
    messages = parse_meta_instagram_messaging(payload)
    assert len(messages) == 1
    assert messages[0].text == "teste"
    assert messages[0].sender_external_id == "user-2"
    assert messages[0].provider == "meta"


def test_parse_instagram_login_from_and_string_message():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-biz",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "from": {"id": "user-9", "username": "cliente"},
                            "id": "mid-9",
                            "message": "oi, quanto fica?",
                            "timestamp": 1710000000,
                        },
                    }
                ],
            }
        ],
    }
    messages = parse_meta_instagram_messaging(payload)
    assert len(messages) == 1
    assert messages[0].text == "oi, quanto fica?"
    assert messages[0].sender_external_id == "user-9"
    assert messages[0].message_id == "mid-9"
    assert messages[0].provider == "meta"
    assert messages[0].sender_username == "cliente"


def test_parse_message_edit_with_nested_sender_and_text():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-biz",
                "messaging": [
                    {
                        "timestamp": 1,
                        "message_edit": {
                            "mid": "m-edit",
                            "text": "oi editado",
                            "from": {"id": "user-edit"},
                        },
                    }
                ],
            }
        ],
    }
    shapes = messaging_event_shapes(payload)
    assert shapes[0]["edit_text_len"] == 10
    assert shapes[0]["edit_has_sender"] is True
    assert parse_meta_instagram_messaging(payload) == []
    assert (
        instagram_event_skip_reason(payload["entry"][0]["messaging"][0])
        == "message_edit_unparsed"
    )


def test_messaging_event_shapes_marks_read_receipts():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-biz",
                "messaging": [
                    {
                        "sender": {"id": "user-1"},
                        "recipient": {"id": "ig-biz"},
                        "timestamp": 1,
                        "read": {"watermark": 1},
                    }
                ],
            }
        ],
    }
    shapes = messaging_event_shapes(payload)
    assert shapes[0]["has_read"] is True
    assert shapes[0]["text_len"] == 0
    assert parse_meta_instagram_messaging(payload) == []
    assert instagram_event_skip_reason(payload["entry"][0]["messaging"][0]) == "read_receipt"
    skeleton = payload_skeleton(payload)
    assert "messaging" in skeleton["entry"][0]


def test_parse_meta_story_reply_attachment():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-biz",
                "messaging": [
                    {
                        "sender": {"id": "user-1"},
                        "recipient": {"id": "ig-biz"},
                        "message": {
                            "mid": "m1",
                            "text": "qual o tamanho?",
                            "reply_to": {
                                "story": {
                                    "id": "story-99",
                                    "url": "https://cdn.example/story.jpg",
                                }
                            },
                        },
                    }
                ],
            }
        ],
    }
    messages = parse_meta_instagram_messaging(payload)
    assert len(messages) == 1
    msg = messages[0]
    assert msg.channel == "instagram"
    assert msg.provider == "meta"
    assert msg.text == "qual o tamanho?"
    assert msg.image_url == "https://cdn.example/story.jpg"
    assert msg.instagram_story is not None
    assert msg.instagram_story.replied_to_story is True
    assert msg.instagram_story.story_media_id == "story-99"


def test_parse_meta_video_story_and_link_sticker():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-biz",
                "messaging": [
                    {
                        "sender": {"id": "user-1"},
                        "recipient": {"id": "ig-biz"},
                        "message": {
                            "mid": "m-video",
                            "text": "tem outra cor?",
                            "reply_to": {
                                "story": {
                                    "id": "story-video",
                                    "url": "https://scontent.cdninstagram.com/v/t50.2886-16/x.mp4?oe=ABC",
                                    "thumbnail_url": "https://scontent.cdninstagram.com/v/t51/thumb.jpg?oe=ABC",
                                    "link_sticker_url": (
                                        "https://www.newstorerj.com/relogios/"
                                        "relogios-laco/relogio-laco-pilot-leipzig-mecanico-preto"
                                    ),
                                }
                            },
                        },
                    }
                ],
            }
        ],
    }
    messages = parse_meta_instagram_messaging(payload)
    assert len(messages) == 1
    story = messages[0].instagram_story
    assert story is not None
    assert story.media_type == "video"
    assert story.operational_thumbnail_url() is not None
    assert story.story_link_sticker_url and "laco-pilot" in story.story_link_sticker_url


def _manychat_event(text, *, story=None, echo=False):
    message = {'mid': 'manychat-test', 'text': text}
    if story is not None:
        message['reply_to'] = {'story': story}
    if echo:
        message['is_echo'] = True
    return {'sender': {'id': 'user-test', 'username': 'cliente'},
            'recipient': {'id': 'ig-biz'}, 'message': message}


@pytest.mark.parametrize('text', ['valor', 'VALOR', ' Valor \n'])
@pytest.mark.parametrize('envelope', ['messaging', 'standby', 'changes'])
def test_manychat_owns_exact_story_keyword_only(text, envelope, monkeypatch):
    events = []
    monkeypatch.setattr('app.channels.meta_instagram.log_event', lambda name, data: events.append((name, data)))
    def unexpected_lookup(_):
        pytest.fail('ManyChat keyword must be filtered before profile/API work')
    monkeypatch.setattr('app.channels.meta_instagram._lookup_ig_username', unexpected_lookup)
    event = _manychat_event(text, story={'id': 'story-99'})
    entry = ({'changes': [{'field': 'messages', 'value': event}]} if envelope == 'changes'
             else {envelope: [event]})
    assert instagram_event_skip_reason(event) == 'manychat_story_keyword'
    assert parse_meta_instagram_messaging({'entry': [entry]}) == []
    assert any(data.get('reason') == 'manychat_story_keyword' for _, data in events)


@pytest.mark.parametrize('text,story', [
    ('valor', None), ('valor', {}), ('qual o valor?', {'id': 'story-99'}),
    ('valor e prazo?', {'id': 'story-99'}), ('tem safira?', {'id': 'story-99'}),
    ('valor?', {'id': 'story-99'}),
])
def test_manychat_filter_preserves_other_questions_and_regular_dms(text, story):
    event = _manychat_event(text, story=story)
    assert instagram_event_skip_reason(event) == 'parsed'
    messages = parse_meta_instagram_messaging({'entry': [{'messaging': [event]}]})
    assert len(messages) == 1 and messages[0].text == text


def test_manychat_echo_is_ignored_without_pausing_followup():
    keyword = _manychat_event('valor', story={'id': 'story-99'})
    echo = _manychat_event('Custa R$ 100', echo=True)
    followup = _manychat_event('E o prazo de entrega?')
    assert instagram_event_skip_reason(echo) == 'echo'
    messages = parse_meta_instagram_messaging({'entry': [{'messaging': [keyword, echo, followup]}]})
    assert [m.text for m in messages] == ['E o prazo de entrega?']


def test_story_rollout_allows_meta_live_media(monkeypatch):
    from app.config import get_settings
    from app.stories.instagram_story_models import InstagramStoryContext
    from app.stories.instagram_story_service import story_rollout_allows
    from app.models import IncomingMessage
    from pydantic import SecretStr

    get_settings.cache_clear()
    monkeypatch.setenv("META_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_STORY_RECOGNITION_ENABLED", "false")
    monkeypatch.setenv("INSTAGRAM_STORY_ROLLOUT_MODE", "off")
    get_settings.cache_clear()

    story = InstagramStoryContext(
        provider="meta",
        instagram_account_id="17841404241547355",
        story_media_id="s1",
        replied_to_story=True,
        story_media_url_private=SecretStr("https://cdn.example/s.jpg"),
    )
    incoming = IncomingMessage(
        provider="meta",
        channel="instagram",
        image_url="https://cdn.example/s.jpg",
        instagram_story=story,
    )
    allowed, reason = story_rollout_allows(
        tenant_id="newstore",
        story=story,
        incoming=incoming,
    )
    assert allowed is True
    assert reason == "meta_live_media"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_meta_send_keeps_access_token_out_of_url(monkeypatch):
    from app.channels import meta_instagram as module
    from app.models import AgentResult, IncomingMessage

    calls = []
    class Response:
        status_code = 200
        content = b'{"message_id":"ok"}'
        def json(self):
            return {"message_id": "ok"}
    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr("httpx.AsyncClient", Client)
    monkeypatch.setattr(module, "get_settings", lambda: type("S", (), {
        "meta_page_access_token": "IGAA-secret-token",
        "meta_ig_business_account_id": "business-id",
    })())
    result = await module.send_meta_instagram_reply(
        IncomingMessage(sender_external_id="recipient", sender_key="instagram:recipient"),
        AgentResult(reply_text="Olá"),
    )
    assert result["ok"] is True
    assert calls
    assert all("IGAA-secret-token" not in url for url, _ in calls)
    assert all("params" not in kwargs for _, kwargs in calls)
    assert calls[0][1]["headers"]["Authorization"] == "Bearer IGAA-secret-token"
