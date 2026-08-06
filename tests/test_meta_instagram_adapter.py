from app.channels.meta_instagram import (
    handle_meta_verify_challenge,
    parse_meta_instagram_messaging,
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
                            "text": "valor",
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
    assert msg.text == "valor"
    assert msg.image_url == "https://cdn.example/story.jpg"
    assert msg.instagram_story is not None
    assert msg.instagram_story.replied_to_story is True
    assert msg.instagram_story.story_media_id == "story-99"
