from __future__ import annotations

import app.persona.persona_repository as repo
from tests.persona.persona_fakes import InMemoryPersonaStore
from uuid import UUID


def test_postgres_uuid_workspace_survives_persona_mapping():
    workspace_id = UUID("b3d7eed2-1cd0-45ac-82c7-8a8e8c0430b8")
    persona = repo._row_to_persona({
        "id": 17, "tenant_id": "tenant", "persona_key": "commercial",
        "workspace_id": workspace_id, "version": 1, "name": "Agent",
        "instructions": "Database persona", "instructions_hash": "hash",
    })
    assert persona.workspace_id == str(workspace_id)
    assert persona.model_dump(mode="json")["workspace_id"] == str(workspace_id)


def test_hash_instructions_stable():
    text = "Você é o assistente comercial oficial da NewStore.\n"
    assert repo.hash_instructions(text) == repo.hash_instructions(text)
    assert repo.hash_instructions(text) != repo.hash_instructions(text + "x")


def test_create_draft_activate_archive_rollback(monkeypatch):
    store = InMemoryPersonaStore().install(monkeypatch)

    v1 = repo.create_persona_version(
        instructions="persona v1\n",
        name="V1",
        created_by="test",
    )
    assert v1.status == "draft"
    assert v1.version == 1

    active = repo.activate_persona_version(v1.id, activated_by="ops")
    assert active.status == "active"
    assert repo.get_active_persona().id == v1.id

    v2 = repo.create_persona_version(
        instructions="persona v2\n",
        name="V2",
        created_by="test",
    )
    repo.activate_persona_version(v2.id, activated_by="ops")
    assert repo.get_active_persona().id == v2.id
    archived_v1 = next(p for p in repo.list_persona_versions() if p.id == v1.id)
    assert archived_v1.status == "archived"

    rolled = repo.rollback_persona_version(v1.id, activated_by="ops")
    assert rolled.id == v1.id
    assert rolled.status == "active"
    assert repo.get_active_persona().id == v1.id

    repo.archive_persona_version(v1.id)
    assert repo.get_active_persona() is None
    assert len([p for p in store.personas if p["status"] == "active"]) == 0


def test_only_one_active_per_tenant_persona(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    a = repo.create_persona_version(instructions="a\n", name="A")
    b = repo.create_persona_version(instructions="b\n", name="B")
    repo.activate_persona_version(a.id)
    repo.activate_persona_version(b.id)
    actives = [p for p in repo.list_persona_versions() if p.status == "active"]
    assert len(actives) == 1
    assert actives[0].id == b.id
