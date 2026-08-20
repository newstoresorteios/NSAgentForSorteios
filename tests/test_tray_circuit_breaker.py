import pytest

from app.tray_adapter_client import TrayAdapterClient, TrayAdapterError
from app.tray_circuit_breaker import (
    TrayCircuitBreaker,
    get_tray_circuit_breaker,
    reset_tray_circuit_breaker_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_breaker():
    reset_tray_circuit_breaker_for_tests()
    yield
    reset_tray_circuit_breaker_for_tests()


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"products": []}

    def json(self):
        return self._payload


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_auth_503_is_not_retried(monkeypatch):
    import app.tray_adapter_client as tray_client

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(tray_client.asyncio, "sleep", no_wait)
    fake = SequenceClient(
        [
            FakeResponse(
                status_code=503,
                payload={"success": False, "error": "tray_authentication_failed"},
            )
        ]
    )
    with pytest.raises(TrayAdapterError) as error:
        await TrayAdapterClient(
            "https://tray.example",
            "secret",
            fake,
        ).search_products(name="Bulova", limit=5)
    assert error.value.status_code == 503
    assert error.value.error == "tray_authentication_failed"
    assert len(fake.calls) == 1
    snap = get_tray_circuit_breaker().snapshot()
    assert snap.state == "open"


@pytest.mark.asyncio
async def test_circuit_open_fails_fast_without_http(monkeypatch):
    import app.tray_adapter_client as tray_client

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(tray_client.asyncio, "sleep", no_wait)
    breaker = get_tray_circuit_breaker()
    breaker.record_failure(
        status_code=503,
        error="tray_authentication_failed",
        force_open=True,
    )

    class BoomClient:
        async def request(self, *args, **kwargs):
            raise AssertionError("HTTP must not run while circuit is open")

    with pytest.raises(TrayAdapterError) as error:
        await TrayAdapterClient(
            "https://tray.example",
            "secret",
            BoomClient(),
        ).search_products(brand="Seiko", limit=5)
    assert error.value.status_code == 503
    assert error.value.error == "tray_circuit_open"


def test_breaker_opens_after_threshold_generic_503():
    breaker = TrayCircuitBreaker(failure_threshold=3, open_seconds=30, enabled=True)
    breaker.record_failure(status_code=503, error="tray_adapter_http_503")
    breaker.record_failure(status_code=503, error="tray_adapter_http_503")
    assert breaker.snapshot().state == "closed"
    breaker.record_failure(status_code=503, error="tray_adapter_http_503")
    assert breaker.snapshot().state == "open"
    assert breaker.allow_request() is False
