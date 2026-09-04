from pathlib import Path

from app.identity.vip_profiles import get_vip_profile
from app.models import IncomingMessage
from app.openai_agent import build_agent_input


_BANNED = (
    "Dorso Livre",
    "Descamisado",
    "Big Boss",
    "FELIPE_NEWBOLD",
    "Atendimento VIP",
    "Cliente VIP",
)


def test_get_vip_profile_does_not_special_case_any_phone():
    assert get_vip_profile("21969544700") is None
    assert get_vip_profile("+55 21 96954-4700") is None
    assert get_vip_profile("85999498149") is None
    assert get_vip_profile("5585999498149") is None
    assert get_vip_profile("5548999490859") is None


def test_live_path_has_no_felipe_vip_copy():
    roots = (
        Path("app/identity"),
        Path("app/llm/agent_replies.py"),
        Path("app/openai_agent.py"),
    )
    hits: list[str] = []
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(root.rglob("*.py"))
    for path in files:
        text = path.read_text(encoding="utf-8")
        for banned in _BANNED:
            if banned in text:
                hits.append(f"{path.as_posix()}:{banned}")
    assert hits == []


def test_runtime_has_no_get_vip_profile_call_sites():
    hits: list[str] = []
    for path in Path("app").rglob("*.py"):
        if path.name == "vip_profiles.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "get_vip_profile" in text:
            hits.append(path.as_posix())
    assert hits == []


def test_build_agent_input_has_no_vip_block_for_founder_phone():
    text = build_agent_input(
        IncomingMessage(text="oi", sender_phone="21969544700"),
        {},
        {"primary_intent": "greeting"},
    )
    lowered = text.casefold()
    assert "vip" not in lowered
    assert "dorso" not in lowered
    assert "descamisado" not in lowered
    assert "big boss" not in lowered
    assert "felipe newbold" not in lowered
