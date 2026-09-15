from __future__ import annotations

from contextvars import ContextVar
from typing import Any
from string import Formatter

_effective_settings: ContextVar[Any] = ContextVar("effective_settings", default=None)
_catalog: ContextVar[dict[str, Any] | None] = ContextVar("operator_catalog", default=None)


class ConfigurationUnavailable(RuntimeError):
    pass


def effective_settings():
    return _effective_settings.get()


def bind_bundle(bundle: dict[str, Any], settings: Any):
    return (_catalog.set(bundle), _effective_settings.set(settings))


def reset_bundle(tokens):
    _catalog.reset(tokens[0])
    _effective_settings.reset(tokens[1])


def current_bundle() -> dict[str, Any]:
    return _catalog.get() or {}


def policy(name: str, *, bundle: dict[str, Any] | None = None) -> Any:
    values = (bundle if bundle is not None else current_bundle()).get("values") or {}
    if name not in values:
        raise ConfigurationUnavailable(f"Missing published policy: {name}")
    return values[name]


def message(name: str, /, **values: Any) -> str:
    template = policy("message." + name)
    if not isinstance(template, str):
        raise ConfigurationUnavailable(f"Invalid message template: {name}")
    required = {field for _, field, _, _ in Formatter().parse(template) if field}
    if required - values.keys():
        raise ConfigurationUnavailable(f"Missing template variables: {name}")
    # Only simple named slots; no attribute or index evaluation.
    if any(not field.isidentifier() for field in required):
        raise ConfigurationUnavailable(f"Unsafe template variables: {name}")
    return template.format_map(values)


def settings_from_bundle(base: Any, bundle: dict[str, Any]):
    from app.core.config import Settings
    payload = base.model_dump(by_alias=True)
    values = bundle.get("values") or {}
    for definition in bundle.get("fields") or []:
        if definition.get("target") != "setting":
            continue
        attribute = definition.get("attribute")
        field = Settings.model_fields.get(attribute)
        if field is None or definition["key"] not in values:
            continue
        # Credential and authority boundaries are not editable business policies.
        if not is_operator_setting(attribute):
            continue
        payload[field.alias or attribute] = values[definition["key"]]
    return Settings(_env_file=None, **payload)


def is_operator_setting(attribute: str) -> bool:
    return not (
        any(term in attribute for term in ("secret", "database_url", "allowlist", "webhook"))
        or attribute.endswith(("_token", "_api_key", "_service_key", "_url"))
        or attribute.startswith(("supabase_", "meta_", "brevo_"))
        or attribute in {"environment", "dry_run", "auto_create_tables", "agent_persona_key",
                         "agent_persona_tenant_id", "agent_db_persona_enabled",
                         "openai_use_previous_response_id", "openai_use_conversations_api"}
    )
