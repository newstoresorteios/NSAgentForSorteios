import pytest

from app.core import remote_media
from app.core.remote_media import RemoteMediaError, validate_remote_media_url


def test_remote_media_rejects_http_and_localhost():
    with pytest.raises(RemoteMediaError) as http_err:
        validate_remote_media_url("http://cdninstagram.com/a.jpg")
    assert http_err.value.code == "scheme_not_https"
    with pytest.raises(RemoteMediaError) as local_err:
        validate_remote_media_url("https://localhost/a.jpg")
    assert local_err.value.code in {"host_blocked", "host_not_allowed", "private_ip_blocked"}


def test_remote_media_rejects_unknown_host():
    with pytest.raises(RemoteMediaError) as err:
        validate_remote_media_url("https://evil.example/a.jpg")
    assert err.value.code == "host_not_allowed"


def test_operator_media_hosts_are_sanitized_and_extend_download_allowlist(monkeypatch):
    monkeypatch.setattr(
        remote_media,
        "policy",
        lambda name: '["storage.googleapis.com", "HTTPS://invalid", "127.0.0.1"]',
    )
    assert remote_media.operator_allowed_media_suffixes() == ("storage.googleapis.com",)
    assert remote_media.validate_remote_media_url(
        "https://storage.googleapis.com/bucket/watch.jpg",
        allowed_suffixes=("storage.googleapis.com",),
        resolver=lambda *_a, **_k: [(None, None, None, None, ("142.250.0.1", 443))],
    ).endswith("watch.jpg")
