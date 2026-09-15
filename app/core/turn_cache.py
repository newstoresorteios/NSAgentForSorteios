"""Memoize immutable configuration reads within exactly one agent turn."""
from __future__ import annotations
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import json

_cache: ContextVar[dict | None] = ContextVar("turn_read_cache", default=None)


def begin_turn_cache():
    return _cache.set({})


def end_turn_cache(token):
    _cache.reset(token)


def invalidates_turn_reads(fn):
    @wraps(fn)
    def write(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            cache = _cache.get()
            if cache is not None:
                cache.clear()
    return write


def cached_turn_read(fn):
    @wraps(fn)
    def read(*args, **kwargs):
        cache = _cache.get()
        if cache is None:
            return fn(*args, **kwargs)
        key = (fn.__module__, fn.__name__, json.dumps([args, kwargs], sort_keys=True, default=str))
        if key not in cache:
            cache[key] = deepcopy(fn(*args, **kwargs))
        else:
            from app.ops.runtime_context import get_current_turn
            runtime = get_current_turn()
            if runtime is not None:
                runtime.iq_counters["turn_read_cache_hits"] = runtime.iq_counters.get("turn_read_cache_hits", 0) + 1
        return deepcopy(cache[key])
    return read
