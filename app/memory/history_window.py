"""Separate operational conversation history from the model prompt window.

IQ-09: one canonical model window (``agent_history_limit``).
``agent_max_recent_turns`` is kept as a deprecated alias and synced at settings load.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo

    _LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    _LOCAL_TZ = timezone(timedelta(hours=-3))
_SENT_PREFIX = "[enviada "

HISTORY_TIME_POLICY = """
Cada turno do histórico pode começar com [enviada em AAAA-MM-DD HH:MM] (horário de Brasília) ou [enviada agora].
Use o horário para ver o que é atendimento antigo no meio do fio.
Não carregue cor, estilo, orçamento, produto ou pedido de turnos antigos como restrição do turno atual, a menos que a mensagem atual reafirme.
A mensagem atual está marcada como [enviada agora].
""".strip()


def resolve_history_hard_cap(settings: Any | None = None) -> int:
    """DB / operational recovery bound (not the LLM prompt window)."""
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    try:
        value = int(getattr(settings, "agent_history_hard_cap", 80) or 80)
    except (TypeError, ValueError):
        value = 80
    return max(8, min(200, value))


def resolve_model_history_limit(settings: Any | None = None) -> int:
    """Canonical turns sent to interpreter / responder / persona / critique.

    Prefer ``agent_history_limit``. Fall back to ``agent_max_recent_turns`` only when
    history_limit is missing (legacy callers / partial stubs in tests).
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    raw = getattr(settings, "agent_history_limit", None)
    if raw is None:
        raw = getattr(settings, "agent_max_recent_turns", 12)
    try:
        value = int(raw or 12)
    except (TypeError, ValueError):
        value = 12
    hard_cap = resolve_history_hard_cap(settings)
    return max(4, min(hard_cap, value))


def select_model_history_turns(
    turns: list[dict[str, Any]] | None,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Return the newest ``limit`` turns for LLM prompts.

    Operational recovery should keep the full loaded window (hard cap).
    The model only receives this sliced list.
    """
    if not turns:
        return []
    safe_limit = max(0, int(limit))
    if safe_limit <= 0:
        return []
    return list(turns)[-safe_limit:]


def count_user_assistant_turns(turns: list[dict[str, Any]] | None) -> dict[str, int]:
    values = turns or []
    return {
        "total": len(values),
        "user": sum(1 for turn in values if turn.get("role") == "user"),
        "assistant": sum(1 for turn in values if turn.get("role") == "assistant"),
    }


def _turn_conversation_id(turn: dict[str, Any] | None) -> str:
    if not isinstance(turn, dict):
        return ""
    raw = turn.get("conversation_id")
    if not raw and isinstance(turn.get("metadata"), dict):
        raw = turn["metadata"].get("conversation_id")
    return str(raw or "").strip()


def turns_for_conversation(
    turns: list[dict[str, Any]] | None,
    conversation_id: str | None,
    *,
    include_other_threads: bool = False,
) -> list[dict[str, Any]]:
    """Keep this WhatsApp thread. Untagged turns stay (unit tests / sparse rows).

    When a sale is already open, pass include_other_threads=True so a new Brevo
    conversation_id on the same phone still sees the shortlist / qualification.
    """
    values = list(turns or [])
    if include_other_threads:
        return values
    wanted = str(conversation_id or "").strip()
    if not wanted:
        return values
    scoped: list[dict[str, Any]] = []
    for turn in values:
        cid = _turn_conversation_id(turn)
        if not cid or cid == wanted:
            scoped.append(turn)
    return scoped


def coerce_turn_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def format_message_sent_at(value: Any) -> str | None:
    """Compact Brasília time for GPT history, or None when unknown."""
    if value is None:
        return None
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return text[:16]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(_LOCAL_TZ)
    return local.strftime("%Y-%m-%d %H:%M")


def prefix_turn_sent_at(content: str, sent_at: Any = None, *, current: bool = False) -> str:
    text = str(content or "").strip()
    if not text or text.startswith(_SENT_PREFIX):
        return text
    if current:
        return f"[enviada agora]\n{text}"
    stamp = format_message_sent_at(sent_at)
    if not stamp:
        return text
    return f"[enviada em {stamp}]\n{text}"
