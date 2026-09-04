"""Compatibility shim — canonical module is ``app.core.security``."""

from __future__ import annotations

import sys

from app.core import security as _security

sys.modules[__name__] = _security
