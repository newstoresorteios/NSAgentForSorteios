"""Shared infrastructure: settings, DTOs, Postgres, webhook auth.

Canonical home for modules that used to sit loose on ``app/``.
Import paths ``app.config``, ``app.models``, ``app.db`` and ``app.security``
stay as compatibility shims so tests and monkeypatches keep working.
"""
