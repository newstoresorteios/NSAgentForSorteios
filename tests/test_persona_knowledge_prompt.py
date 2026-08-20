from __future__ import annotations

from app.persona_knowledge_repository import (
    format_persona_knowledge_block,
    format_relevant_knowledge_block,
    load_persona_knowledge_for_prompt,
    text_already_embedded,
)
from app.persona_models import PersonaVersion


def test_text_already_embedded_detects_full_overlap():
    body = "Alpha " * 40 + "Omega " * 10
    instructions = "prefix " + body + " suffix"
    assert text_already_embedded(body, instructions) is True


def test_text_already_embedded_false_for_partial_overlap():
    body = "Alpha beta gamma delta unique tail marker"
    instructions = "Alpha beta gamma delta something else entirely"
    assert text_already_embedded(body, instructions) is False


def test_format_relevant_knowledge_block():
    block = format_relevant_knowledge_block(
        [{"title": "Frete", "body": "Entregamos em todo o Brasil."}]
    )
    assert "<retrieved_knowledge>" in block
    assert "Frete" in block
    assert "tools" in block.casefold()


def test_load_persona_knowledge_skips_embedded_attachment(monkeypatch):
    attachment = {
        "id": "81768596-1e86-4496-a14f-27b60da87aa9",
        "filename": "persona.txt",
        "extracted_text": "KNOWLEDGE_DOC_START " + ("x" * 300) + " KNOWLEDGE_DOC_END",
        "content_type": "text/plain",
    }
    persona = PersonaVersion(
        id=17,
        tenant_id="newstore",
        persona_key="newstore_commercial",
        version=17,
        name="Crono",
        instructions="intro " + attachment["extracted_text"] + " outro",
        instructions_hash="abc",
        status="active",
        metadata={"chatboPersonaId": "3ad8c78a-909b-4631-97be-337222440220"},
    )

    import app.persona_knowledge_repository as repo

    monkeypatch.setattr(repo, "list_persona_attachments", lambda *_a, **_k: [])
    monkeypatch.setattr(repo, "get_chatbo_persona_profile", lambda *_a, **_k: None)

    ids, block = load_persona_knowledge_for_prompt(persona)
    assert ids == []
    assert block == "<persona_knowledge>\n</persona_knowledge>"


def test_load_persona_knowledge_includes_new_attachment(monkeypatch):
    persona = PersonaVersion(
        id=17,
        tenant_id="newstore",
        persona_key="newstore_commercial",
        version=17,
        name="Crono",
        instructions="Persona base curta.",
        instructions_hash="abc",
        status="active",
        metadata={"chatboPersonaId": "3ad8c78a-909b-4631-97be-337222440220"},
    )
    from app.persona_knowledge_repository import PersonaKnowledgeAttachment
    import app.persona_knowledge_repository as repo

    att = PersonaKnowledgeAttachment(
        id="att-1",
        filename="politicas.txt",
        extracted_text="Política exclusiva: seminovos passam por revisão técnica de 42 pontos.",
    )
    monkeypatch.setattr(repo, "list_persona_attachments", lambda *_a, **_k: [att])
    monkeypatch.setattr(
        repo,
        "get_chatbo_persona_profile",
        lambda *_a, **_k: {"objection_handling": {"items": ["Prazo: consultar Tray."]}},
    )

    ids, block = load_persona_knowledge_for_prompt(persona, max_chars=5000)
    assert ids == ["att-1"]
    assert "<persona_knowledge>" in block
    assert "politicas.txt" in block
    assert "seminovos passam por revisão técnica" in block
    assert "Tratamento de objeções" in block or "Prazo: consultar Tray" in block


def test_format_persona_knowledge_block_truncates():
    block = format_persona_knowledge_block(
        attachment_sections=[("big.txt", "a" * 500)],
        profile_text="profile",
        max_chars=120,
    )
    assert "[truncado]" in block
