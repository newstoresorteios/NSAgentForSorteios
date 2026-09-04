"""Compatibility shim — canonical module is ``app.agents.door``."""

from __future__ import annotations

import sys

import app.agents.door as _door

sys.modules[__name__] = _door
