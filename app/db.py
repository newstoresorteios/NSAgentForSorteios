"""Compatibility shim — canonical module is ``app.core.db``."""

from __future__ import annotations

import sys

from app.core import db as _db

sys.modules[__name__] = _db
