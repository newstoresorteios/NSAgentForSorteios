"""Compatibility module: catalog retrieval lives in ``app.catalog.retrieval``."""

from __future__ import annotations

from openai import APIError, AsyncOpenAI

from app.catalog.retrieval import *  # noqa: F403
from app.catalog.retrieval.runtime import get_settings
from app.catalog.retrieval import __all__ as _RETRIEVAL_ALL

__all__ = [*_RETRIEVAL_ALL, "APIError", "AsyncOpenAI", "get_settings"]
