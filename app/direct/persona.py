"""Published persona instructions, without executing legacy decision pipelines."""
import hashlib
import json


def persona_context(persona):
    from app.persona.persona_knowledge_repository import iter_structured_persona_sections
    active = persona.active_persona
    profile = persona.chatbo_profile or {}
    # Explicit content fields only: no credentials, runtime config or client data.
    payload = {
        "version": persona.persona_version_id,
        "instructions": getattr(active, "instructions", "") or "",
        "profile": dict(iter_structured_persona_sections(profile)),
    }
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return {"content": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
