import pytest

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
