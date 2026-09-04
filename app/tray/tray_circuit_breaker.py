"""Process-local circuit breaker for TrayAdaptor HTTP storms.

When Tray returns repeated 503s (especially tray_authentication_failed),
parallel product probes amplify load. Opening the circuit fails fast so
catalog-index / storefront fallbacks can serve the turn.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Literal


CircuitState = Literal["closed", "open", "half_open"]


@dataclass
class CircuitSnapshot:
    state: CircuitState
    failure_count: int
    opened_until: float | None
    last_failure_error: str | None
    blocked_calls: int
    successful_calls: int
    failed_calls: int


class TrayCircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        open_seconds: float = 45.0,
        half_open_successes: int = 1,
        enabled: bool = True,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.open_seconds = max(1.0, float(open_seconds))
        self.half_open_successes = max(1, int(half_open_successes))
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._state: CircuitState = "closed"
        self._failure_count = 0
        self._half_open_successes = 0
        self._opened_until = 0.0
        self._last_failure_error: str | None = None
        self._blocked_calls = 0
        self._successful_calls = 0
        self._failed_calls = 0

    def reset(self) -> None:
        with self._lock:
            self._state = "closed"
            self._failure_count = 0
            self._half_open_successes = 0
            self._opened_until = 0.0
            self._last_failure_error = None

    def snapshot(self) -> CircuitSnapshot:
        with self._lock:
            self._maybe_transition_unlocked(time.monotonic())
            return CircuitSnapshot(
                state=self._state,
                failure_count=self._failure_count,
                opened_until=self._opened_until or None,
                last_failure_error=self._last_failure_error,
                blocked_calls=self._blocked_calls,
                successful_calls=self._successful_calls,
                failed_calls=self._failed_calls,
            )

    def allow_request(self) -> bool:
        if not self.enabled:
            return True
        with self._lock:
            now = time.monotonic()
            self._maybe_transition_unlocked(now)
            if self._state == "open":
                self._blocked_calls += 1
                return False
            return True

    def record_success(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._successful_calls += 1
            if self._state == "half_open":
                self._half_open_successes += 1
                if self._half_open_successes >= self.half_open_successes:
                    self._state = "closed"
                    self._failure_count = 0
                    self._half_open_successes = 0
                    self._opened_until = 0.0
                    self._last_failure_error = None
                return
            self._failure_count = 0
            self._last_failure_error = None

    def record_failure(
        self,
        *,
        status_code: int | None = None,
        error: str | None = None,
        force_open: bool = False,
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._failed_calls += 1
            self._last_failure_error = (error or "").strip() or None
            auth_fail = _is_auth_failure(status_code, error)
            # Auth failures trip faster: one is enough to open.
            if force_open or auth_fail:
                self._trip_open_unlocked(time.monotonic())
                return
            if status_code in {502, 503, 504} or status_code is None:
                self._failure_count += 1
                if self._state == "half_open" or self._failure_count >= self.failure_threshold:
                    self._trip_open_unlocked(time.monotonic())

    def _trip_open_unlocked(self, now: float) -> None:
        self._state = "open"
        self._opened_until = now + self.open_seconds
        self._half_open_successes = 0

    def _maybe_transition_unlocked(self, now: float) -> None:
        if self._state == "open" and now >= self._opened_until:
            self._state = "half_open"
            self._half_open_successes = 0


def _is_auth_failure(status_code: int | None, error: str | None) -> bool:
    text = (error or "").strip().casefold()
    if "tray_authentication_failed" in text or "authentication" in text:
        return True
    if status_code == 503 and text in {
        "tray_authentication_failed",
        "tray_configuration_error",
    }:
        return True
    return False


_breaker: TrayCircuitBreaker | None = None
_breaker_lock = threading.Lock()


def get_tray_circuit_breaker() -> TrayCircuitBreaker:
    global _breaker
    with _breaker_lock:
        if _breaker is None:
            from app.config import get_settings

            settings = get_settings()
            _breaker = TrayCircuitBreaker(
                failure_threshold=int(
                    getattr(settings, "tray_circuit_failure_threshold", 3) or 3
                ),
                open_seconds=float(
                    getattr(settings, "tray_circuit_open_seconds", 45.0) or 45.0
                ),
                enabled=bool(getattr(settings, "tray_circuit_breaker_enabled", True)),
            )
        return _breaker


def reset_tray_circuit_breaker_for_tests() -> None:
    global _breaker
    with _breaker_lock:
        if _breaker is not None:
            _breaker.reset()
        _breaker = None


def circuit_status_dict() -> dict[str, Any]:
    snap = get_tray_circuit_breaker().snapshot()
    return {
        "state": snap.state,
        "failure_count": snap.failure_count,
        "last_failure_error": snap.last_failure_error,
        "blocked_calls": snap.blocked_calls,
        "successful_calls": snap.successful_calls,
        "failed_calls": snap.failed_calls,
        "open_seconds_remaining": (
            max(0.0, round(snap.opened_until - time.monotonic(), 1))
            if snap.state == "open" and snap.opened_until
            else 0.0
        ),
    }
