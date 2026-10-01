from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.configuration import workspace
from app.models import IncomingMessage

NEW_STORE = 'b3d7eed2-1cd0-45ac-82c7-8a8e8c0430b8'
OTHER = '10000000-0000-0000-0000-000000000001'


@pytest.fixture
def configured(monkeypatch):
    settings = SimpleNamespace(database_url='configured', agent_db_persona_enabled=True,
                               brevo_workspace_id=NEW_STORE)
    monkeypatch.setattr('app.config.get_settings', lambda: settings)
    monkeypatch.setattr(workspace, 'resolve_conversation_workspace', lambda *a: None)
    return settings


def test_new_brevo_message_uses_server_binding_not_payload(configured):
    incoming = IncomingMessage(provider='brevo', channel='whatsapp', text='Olá',
                               raw={'workspace_id': OTHER})
    assert workspace.resolve_message_workspace(incoming) == NEW_STORE


@pytest.mark.parametrize('provider', ['meta', 'ycloud', ''])
def test_other_providers_do_not_inherit_brevo_workspace(configured, provider):
    assert workspace.resolve_message_workspace(IncomingMessage(provider=provider, channel='whatsapp', text='Oi')) is None


def test_conflicting_existing_conversation_fails_closed(configured, monkeypatch):
    monkeypatch.setattr(workspace, 'resolve_conversation_workspace', lambda *a: OTHER)
    with pytest.raises(ValueError, match='brevo_conversation_workspace_conflict'):
        workspace.resolve_message_workspace(IncomingMessage(provider='brevo', channel='whatsapp', text='Oi'))


def test_ingress_loads_only_bound_persona(configured, monkeypatch):
    bundle = object()
    loader = Mock(return_value=SimpleNamespace(configuration_bundle=bundle))
    monkeypatch.setattr('app.persona.persona_runtime.load_persona_runtime', loader)
    monkeypatch.setattr('app.configuration.runtime.settings_from_bundle', lambda base, b: b)
    incoming = IncomingMessage(provider='brevo', channel='whatsapp', text='Olá')
    assert workspace.ingress_settings(incoming, configured) is bundle
    loader.assert_called_once_with(workspace_id=NEW_STORE)


def test_silent_inbound_uses_same_binding(configured, monkeypatch):
    stamp = Mock()
    monkeypatch.setattr(workspace, 'stamp_inbound_workspace', stamp)
    workspace.stamp_silent_inbound_workspace(IncomingMessage(provider='brevo', channel='whatsapp', text='Oi'), 1)
    stamp.assert_called_once_with(1, NEW_STORE)


def test_missing_binding_preserves_existing_conversation(configured, monkeypatch):
    configured.brevo_workspace_id = ''
    monkeypatch.setattr(workspace, 'resolve_conversation_workspace', lambda *a: OTHER)
    assert workspace.resolve_message_workspace(IncomingMessage(provider='brevo', channel='whatsapp', text='Oi')) == OTHER
