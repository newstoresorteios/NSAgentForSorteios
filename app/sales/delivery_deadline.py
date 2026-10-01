"""Customer date requirements, anchored to message time, never shipping promises."""
from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from app.catalog.retrieval.text import fold_text

LOCAL_TIMEZONE = "America/Sao_Paulo"


def parse_turn_time(value):
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"\d+(?:\.\d+)?", str(value)):
            numeric = float(value)
            return datetime.fromtimestamp(numeric / 1000 if numeric >= 10_000_000_000 else numeric, timezone.utc)
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # A provider time with no offset has unknown timezone; don't silently shift it.
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def preference_update_context(incoming):
    raw = incoming.raw or {}
    sources = [raw.get("meta_event"), raw.get("message"), incoming.channel_metadata, raw]
    if incoming.provider == "brevo" and isinstance(raw.get("messages"), list):
        from app.channels.webhook_parser import select_effective_inbound_message
        sources.insert(0, select_effective_inbound_message(raw))
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("sent_at", "message_sent_at", "timestamp", "createdAt", "created_at"):
            parsed = parse_turn_time(source.get(key))
            if parsed is not None:
                return {"source": "customer_interpretation", "inbound_id": raw.get("inbound_id"),
                        "observed_at": parsed.isoformat(), "time_source": "message", "confidence": None}
    received = parse_turn_time(raw.get("received_at"))
    return {"source": "customer_interpretation", "inbound_id": raw.get("inbound_id"),
            "observed_at": received.isoformat() if received else None,
            "time_source": "received" if received else "unavailable", "confidence": None}


def resolve_delivery_requirement(text, context=None):
    context = context or {}
    reference = parse_turn_time(context.get("observed_at"))
    value = fold_text(text or "")
    result = {"raw_text": str(text or "")[:300], "timezone": LOCAL_TIMEZONE,
              "reference_at": reference.isoformat() if reference else None,
              "time_source": context.get("time_source", "unavailable"),
              "earliest_date": None, "latest_date": None, "resolution": "unresolved",
              "requires_date_confirmation": True, "arrival_confirmed": False}
    start = end = None
    exact = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", value)
    day_month = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\b", value)
    try:
        if exact:
            start = end = date(*map(int, exact.groups()))
            result["requires_date_confirmation"] = False
        elif day_month and (day_month[3] or reference):
            local = reference.astimezone(ZoneInfo(LOCAL_TIMEZONE)) if reference else None
            start = end = date(int(day_month[3]) if day_month[3] else local.year, int(day_month[2]), int(day_month[1]))
            result["requires_date_confirmation"] = not bool(day_month[3])
        elif reference and context.get("time_source") == "message":
            today = reference.astimezone(ZoneInfo(LOCAL_TIMEZONE)).date()
            if re.search(r"\bdepois de amanha\b", value):
                start = end = today + timedelta(days=2)
            elif re.search(r"\bamanha\b", value):
                start = end = today + timedelta(days=1)
            elif re.search(r"\bhoje\b", value):
                start = end = today
            elif re.search(r"\b(?:proxima semana|semana que vem|semana seguinte)\b", value):
                start = today + timedelta(days=7 - today.weekday())
                end = start + timedelta(days=6)
            elif re.search(r"\b(?:proximo (?:fim|final) de semana|(?:fim|final) de semana que vem)\b", value):
                start = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
                end = start + timedelta(days=1)
            if start == end and start:
                result["requires_date_confirmation"] = False
    except ValueError:
        start = end = None
    if start:
        result.update(earliest_date=start.isoformat(), latest_date=end.isoformat(),
                      resolution="date" if start == end else "range")
    return result
