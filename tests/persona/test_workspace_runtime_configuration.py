from types import SimpleNamespace

from app.catalog.retrieval.limits import candidate_pool_limit, rerank_selection_limit
from app.memory.history_window import resolve_model_history_limit
from app.ops.observability import full_obs_enabled
from app.persona.persona_runtime import (
    PersonaRuntimeConfig,
    reset_persona_runtime,
    set_persona_runtime,
)


def test_workspace_overrides_apply_inside_turn_context():
    runtime = PersonaRuntimeConfig(
        runtime_configuration={
            "historyTurns": 18,
            "catalogCandidatePool": 45,
            "catalogRerankLimit": 12,
            "observabilityLevel": "detailed",
        }
    )
    token = set_persona_runtime(runtime)
    try:
        settings = SimpleNamespace(
            agent_history_limit=12,
            agent_max_recent_turns=12,
            agent_history_hard_cap=80,
            agent_candidate_pool_limit=20,
            agent_rerank_selection_limit=15,
        )
        assert resolve_model_history_limit(settings) == 18
        assert candidate_pool_limit() == 45
        assert rerank_selection_limit() == 12
        assert full_obs_enabled() is True
    finally:
        reset_persona_runtime(token)


def test_workspace_overrides_are_clamped_by_runtime_guards():
    runtime = PersonaRuntimeConfig(
        runtime_configuration={
            "historyTurns": 999,
            "catalogCandidatePool": 999,
            "catalogRerankLimit": 1,
        }
    )
    token = set_persona_runtime(runtime)
    try:
        settings = SimpleNamespace(
            agent_history_limit=12,
            agent_max_recent_turns=12,
            agent_history_hard_cap=80,
        )
        assert resolve_model_history_limit(settings) == 80
        assert candidate_pool_limit() == 80
        assert rerank_selection_limit() == 5
    finally:
        reset_persona_runtime(token)
