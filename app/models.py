"""Compatibility shim — canonical module is ``app.core.models``."""

from __future__ import annotations

import sys

from app.core import models as _models

sys.modules[__name__] = _models
