"""Workspace-scoped operator references for watches shown in Story Highlights."""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.core.db import get_conn


_SKIP = {"relogio", "relogios", "watch", "watches", "masculino", "feminino", "automatico", "quartzo"}
_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


def _fold(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    return re.sub(r"[^a-z0-9]+", " ", "".join(ch for ch in decomposed if not unicodedata.combining(ch))).strip()


def _tokens(value: Any) -> set[str]:
    return {token for token in _fold(value).split() if len(token) >= 3 and token not in _SKIP}


@dataclass(frozen=True)
class StoryHighlightReference:
    id: int
    name: str
    product_url: str
    matched_tokens: tuple[str, ...]


class StoryHighlightReferenceRepository:
    def list_active(self, *, workspace_id: str, tenant_id: str) -> list[dict[str, Any]]:
        key = (str(workspace_id), str(tenant_id))
        now = time.monotonic()
        with _cache_lock:
            cached = _cache.get(key)
            if cached and cached[0] > now:
                return list(cached[1])
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id, name, normalized_name, product_url
                   FROM public.ai_story_highlight_references
                  WHERE workspace_id=%s::uuid AND tenant_id=%s AND active=true
                  ORDER BY name LIMIT 500""",
                (workspace_id, tenant_id),
            )
            rows = [dict(row) for row in (cur.fetchall() or [])]
        with _cache_lock:
            _cache[key] = (now + 30.0, rows)
        return rows

    def match_text(self, *, workspace_id: str, tenant_id: str, text: str) -> list[StoryHighlightReference]:
        evidence = _fold(text)
        evidence_tokens = _tokens(evidence)
        if not evidence_tokens:
            return []
        ranked: list[tuple[int, float, dict[str, Any], tuple[str, ...]]] = []
        for row in self.list_active(workspace_id=workspace_id, tenant_id=tenant_id):
            normalized = _fold(row.get("normalized_name") or row.get("name"))
            ref_tokens = _tokens(normalized)
            hits = tuple(sorted(ref_tokens & evidence_tokens))
            if not hits:
                continue
            exact = bool(normalized and re.search(rf"\b{re.escape(normalized)}\b", evidence))
            coverage = len(hits) / max(len(ref_tokens), 1)
            if exact or len(hits) >= 2 or (len(hits) == 1 and len(hits[0]) >= 4):
                ranked.append((2 if exact else len(hits), coverage, row, hits))
        ranked.sort(key=lambda item: (-item[0], -item[1], str(item[2].get("name") or "")))
        if not ranked:
            return []
        best_strength = ranked[0][:2]
        best = [item for item in ranked if item[:2] == best_strength]
        # A short brand-only clue is safe only when it identifies one configured reference.
        if len(best) != 1:
            return []
        _, _, row, hits = best[0]
        if len(hits) == 1:
            competing = [item for item in ranked if item[3] == hits]
            if len(competing) != 1:
                return []
        return [StoryHighlightReference(
            id=int(row["id"]), name=str(row["name"]),
            product_url=str(row["product_url"]), matched_tokens=hits,
        )]


def current_workspace_reference(text: str, *, tenant_id: str) -> StoryHighlightReference | None:
    try:
        from app.persona.persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
        workspace_id = str(getattr(runtime, "workspace_id", None) or "").strip()
        if not workspace_id:
            return None
        matches = StoryHighlightReferenceRepository().match_text(
            workspace_id=workspace_id, tenant_id=tenant_id, text=text,
        )
        return matches[0] if len(matches) == 1 else None
    except Exception as exc:  # schema or database unavailability cannot break sales
        print("[story.highlight_reference.error]", {"error_type": type(exc).__name__})
        return None


def analysis_reference(analysis: Any, *, tenant_id: str) -> StoryHighlightReference | None:
    evidence = " ".join(str(value) for value in [
        *(getattr(analysis, "visible_text", None) or []),
        *(getattr(analysis, "visible_brands", None) or []),
        *(getattr(analysis, "logo_hypotheses", None) or []),
        *(getattr(analysis, "model_hypotheses", None) or []),
        *(getattr(analysis, "collection_hypotheses", None) or []),
        *(getattr(analysis, "visible_references", None) or []),
        getattr(analysis, "visual_description", "") or "",
    ] if value)
    return current_workspace_reference(evidence, tenant_id=tenant_id)
