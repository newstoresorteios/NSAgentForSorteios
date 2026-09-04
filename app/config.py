"""Compatibility shim — canonical module is ``app.core.config``."""

from __future__ import annotations

import sys

from app.core import config as _config

sys.modules[__name__] = _config
