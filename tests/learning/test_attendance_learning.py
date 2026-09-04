"""Unit tests for continuous attendance learning (cursor, council, constitution, canary)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.learning.attendance_learning import (
    classify_attendance,
    classify_pipeline_block,
    promote_insights_to_extensions,
    record_pipeline_block_review,
    run_attendance_learning_batch,
)
from app.learning.cases import format_learned_cases_block, list_active_cases, upsert_learning_case
from app.learning.constitution import check_instruction_delta
from app.learning.rollback import evaluate_canaries


def test_classify_preference_misread_empty_catalog():
    row = {
        "customer_text": "feminino até 3000 reais",
        "agent_reply": "Não encontrei esse produto no catálogo agora.",
        "handoff_required": False,
        "intent": "commerce",
        "response_metadata": {},
    }
    result = classify_attendance(row)
    assert result["outcome"] == "empty_catalog"
    assert "preference_misread" in result["failure_codes"]


def test_classify_trade_in_false_buy_claim():
    row = {
        "customer_text": "vcs estão comprando Certina seminovo?",
        "agent_reply": "Sim, avaliamos e compramos relógios seminovos.",
        "handoff_required": False,
        "intent": "commerce",
        "response_metadata": {},
    }
    result = classify_attendance(row)
    assert result["outcome"] == "policy_miss"
    assert "trade_in_policy_miss" in result["failure_codes"]


def test_classify_trade_in_correct_denial_is_ok():
    row = {
        "customer_text": "vcs estão comprando Certina seminovo?",
        "agent_reply": (
            "A New Store não avalia nem compra relógios de particulares por aqui. "
            "Posso te conectar com um atendente."
        ),
        "handoff_required": True,
        "intent": "commerce",
        "response_metadata": {},
    }
    result = classify_attendance(row)
    assert "trade_in_policy_miss" not in result["failure_codes"]


def test_classify_ignored_model_from_council():
    row = {
        "customer_text": "quero o Baltic MK2",
        "agent_reply": "Separei estas opções da Hermétique.",
        "handoff_required": False,
        "intent": "commerce",
        "response_metadata": {
            "answer_council": {"issues": ["ignored_model"], "approved": False},
        },
    }
    result = classify_attendance(row)
    assert "ignored_model" in result["failure_codes"]
    assert result["outcome"] == "failure"


def test_classify_answer_council_blocked_safety_reason():
    row = {
        "customer_text": "quero o 2",
        "agent_reply": "Qual cidade?",
        "handoff_required": False,
        "intent": "commerce",
        "safety_reason": "answer_council_blocked",
        "response_metadata": {},
    }
    result = classify_attendance(row)
    assert "answer_council_blocked" in result["failure_codes"]
    assert result["outcome"] == "failure"


def test_promote_creates_pending_extension_without_auto_activate(monkeypatch):
    calls = {"approve": 0, "create": 0, "updates": []}

    monkeypatch.setattr(
        "app.learning.promote.get_settings",
        lambda: SimpleNamespace(
            agent_learning_auto_activate=False,
            agent_learning_canary_hours=6,
            agent_learning_max_instruction_chars=800,
        ),
    )
    monkeypatch.setattr(
        "app.learning.constitution.get_settings",
        lambda: SimpleNamespace(agent_learning_max_instruction_chars=800),
    )

    def fake_create(**kwargs):
        calls["create"] += 1
        assert kwargs["extension_key"].startswith("learning:")
        return {"id": 42, "status": "pending_review"}

    def fake_approve(*_a, **_k):
        calls["approve"] += 1
        raise AssertionError("approve_extension must not run when auto_activate=false")

    class FakeCursor:
        def execute(self, sql, params=None):
            calls["updates"].append((sql, params))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("app.learning.promote.create_extension_proposal", fake_create)
    monkeypatch.setattr("app.learning.promote.approve_extension", fake_approve)
    monkeypatch.setattr("app.learning.promote.get_conn", lambda: FakeConn())

    ext_id = promote_insights_to_extensions(
        tenant_id="newstore",
        insight_id=7,
        category="policy",
        insight_text="Não recusar o pedido de seminovo com política inventada; siga o handler de trade-in.",
        confidence=0.8,
        importance=0.7,
    )
    assert ext_id == 42
    assert calls["create"] == 1
    assert calls["approve"] == 0
    assert len(calls["updates"]) == 1
    sql, params = calls["updates"][0]
    assert "status = 'applied'" not in sql
    assert "pending_review" in sql
    assert params == (42, 7)


def test_promote_flag_true_without_review_stays_pending(monkeypatch):
    calls = {"approve": 0}

    monkeypatch.setattr(
        "app.learning.promote.get_settings",
        lambda: SimpleNamespace(
            agent_learning_auto_activate=True,
            agent_learning_canary_hours=6,
            agent_learning_max_instruction_chars=800,
        ),
    )
    monkeypatch.setattr(
        "app.learning.constitution.get_settings",
        lambda: SimpleNamespace(agent_learning_max_instruction_chars=800),
    )
    monkeypatch.setattr(
        "app.learning.promote.create_extension_proposal",
        lambda **_k: {"id": 77, "status": "pending_review"},
    )
    monkeypatch.setattr(
        "app.learning.promote.approve_extension",
        lambda *_a, **_k: calls.update(approve=calls["approve"] + 1) or {"id": 77},
    )

    class FakeCursor:
        def execute(self, sql, params=None):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("app.learning.promote.get_conn", lambda: FakeConn())

    ext_id = promote_insights_to_extensions(
        tenant_id="newstore",
        insight_id=8,
        category="policy",
        insight_text="Não recusar o pedido de seminovo com política inventada; siga o handler de trade-in.",
        confidence=0.8,
        importance=0.7,
    )
    assert ext_id == 77
    assert calls["approve"] == 0


def test_promote_auto_approves_when_flag_true(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(
        "app.learning.promote.get_settings",
        lambda: SimpleNamespace(
            agent_learning_auto_activate=True,
            agent_learning_canary_hours=6,
            agent_learning_max_instruction_chars=800,
        ),
    )
    monkeypatch.setattr(
        "app.learning.constitution.get_settings",
        lambda: SimpleNamespace(agent_learning_max_instruction_chars=800),
    )
    monkeypatch.setattr(
        "app.learning.promote.create_extension_proposal",
        lambda **_k: {"id": 99, "status": "pending_review"},
    )

    def fake_approve(extension_id, *, tenant_id, approved_by, expires_at=None):
        calls["approve"] = {
            "extension_id": extension_id,
            "tenant_id": tenant_id,
            "approved_by": approved_by,
            "expires_at": expires_at,
        }
        return {"id": 99, "status": "active"}

    monkeypatch.setattr("app.learning.promote.approve_extension", fake_approve)
    monkeypatch.setattr(
        "app.learning.promote.mark_insight_status",
        lambda **kwargs: calls.setdefault("mark", kwargs),
    )

    ext_id = promote_insights_to_extensions(
        tenant_id="newstore",
        insight_id=1,
        category="retrieval",
        insight_text=(
            "Quando o cliente pedir um modelo específico, busque esse modelo "
            "no catálogo antes de listar outras famílias."
        ),
        confidence=0.9,
        importance=0.8,
        reviewed=True,
    )
    assert ext_id == 99
    assert calls["approve"]["approved_by"] == "learning_auto"
    assert calls["approve"]["expires_at"] is not None
    assert calls["mark"]["status"] == "applied"


def test_promote_rejects_trade_in_rewrite(monkeypatch):
    monkeypatch.setattr(
        "app.learning.promote.get_settings",
        lambda: SimpleNamespace(
            agent_learning_auto_activate=True,
            agent_learning_canary_hours=6,
            agent_learning_max_instruction_chars=800,
        ),
    )
    monkeypatch.setattr(
        "app.learning.constitution.get_settings",
        lambda: SimpleNamespace(agent_learning_max_instruction_chars=800),
    )
    marked: dict = {}
    monkeypatch.setattr(
        "app.learning.promote.mark_insight_status",
        lambda **kwargs: marked.update(kwargs),
    )
    monkeypatch.setattr(
        "app.learning.promote.create_extension_proposal",
        lambda **_k: (_ for _ in ()).throw(AssertionError("must not create")),
    )
    ext_id = promote_insights_to_extensions(
        tenant_id="newstore",
        insight_id=3,
        category="policy",
        insight_text="A New Store avalia, troca e compra relógios seminovos.",
        confidence=0.9,
        importance=0.8,
    )
    assert ext_id is None
    assert marked["status"] == "rejected"


@pytest.mark.asyncio
async def test_batch_kill_switch_does_not_promote(monkeypatch):
    monkeypatch.setattr(
        "app.learning.attendance_learning.get_settings",
        lambda: SimpleNamespace(
            agent_persona_tenant_id="newstore",
            agent_learning_lookback_hours=24,
            agent_learning_bootstrap_hours=24,
            agent_learning_batch_limit=200,
            agent_learning_auto_promote=False,
            agent_learning_auto_activate=False,
            agent_learning_max_clusters=5,
        ),
    )
    monkeypatch.setattr(
        "app.learning.attendance_learning.evaluate_canaries",
        lambda **_k: {"rolled_back": 0, "confirmed": 0, "extended": 0},
    )
    monkeypatch.setattr(
        "app.learning.attendance_learning.load_cursor",
        lambda **_k: {"last_response_id": 10},
    )
    monkeypatch.setattr(
        "app.learning.attendance_learning.fetch_attendances_since",
        lambda **_k: [],
    )
    monkeypatch.setattr("app.learning.attendance_learning.save_cursor", lambda **_k: None)
    monkeypatch.setattr(
        "app.learning.attendance_learning.compute_fail_rate",
        lambda **_k: (0.0, 0),
    )

    async def boom(**_k):
        raise AssertionError("reflect must not run without clusters")

    monkeypatch.setattr("app.learning.attendance_learning.reflect_cluster", boom)
    monkeypatch.setattr(
        "app.learning.attendance_learning.promote_insights_to_extensions",
        lambda **_k: (_ for _ in ()).throw(AssertionError("promote must not run")),
    )

    summary = await run_attendance_learning_batch()
    assert summary["extensions_promoted"] == 0
    assert summary["insights_upserted"] == 0
    assert summary["reflections"] == 0
    assert summary["cursor_from"] == 10


@pytest.mark.asyncio
async def test_batch_advances_cursor_and_skips_duplicate_reviews(monkeypatch):
    saved: dict = {}
    persist_calls: list[int] = []

    monkeypatch.setattr(
        "app.learning.attendance_learning.get_settings",
        lambda: SimpleNamespace(
            agent_persona_tenant_id="newstore",
            agent_learning_bootstrap_hours=24,
            agent_learning_batch_limit=200,
            agent_learning_auto_promote=True,
            agent_learning_auto_activate=True,
            agent_learning_max_clusters=5,
        ),
    )
    monkeypatch.setattr(
        "app.learning.attendance_learning.evaluate_canaries",
        lambda **_k: {"rolled_back": 0, "confirmed": 0, "extended": 0},
    )
    monkeypatch.setattr(
        "app.learning.attendance_learning.load_cursor",
        lambda **_k: {"last_response_id": 10},
    )
    monkeypatch.setattr(
        "app.learning.attendance_learning.fetch_attendances_since",
        lambda **_k: [
            {
                "response_id": 11,
                "inbound_id": 1,
                "agent_reply": "Separei Hermétique.",
                "intent": "commerce",
                "handoff_required": False,
                "safety_reason": None,
                "response_metadata": {"answer_council": {"issues": ["ignored_model"]}},
                "response_created_at": datetime.now(timezone.utc),
                "sender_key": "wa:1",
                "customer_text": "quero o Baltic MK2",
                "channel": "whatsapp",
                "conversation_id": "c1",
            },
            {
                "response_id": 12,
                "inbound_id": 2,
                "agent_reply": "ok",
                "intent": "commerce",
                "handoff_required": False,
                "safety_reason": None,
                "response_metadata": {},
                "response_created_at": datetime.now(timezone.utc),
                "sender_key": "wa:1",
                "customer_text": "obrigado",
                "channel": "whatsapp",
                "conversation_id": "c1",
            },
        ],
    )

    def fake_persist(*, tenant_id, row, classification):
        persist_calls.append(int(row["response_id"]))
        return int(row["response_id"]), True

    monkeypatch.setattr(
        "app.learning.attendance_learning.persist_attendance_review",
        fake_persist,
    )
    monkeypatch.setattr(
        "app.learning.attendance_learning.save_cursor",
        lambda **kwargs: saved.update(kwargs),
    )
    monkeypatch.setattr(
        "app.learning.attendance_learning.compute_fail_rate",
        lambda **_k: (0.1, 10),
    )

    from app.learning.reflect import ReflectionDelta

    async def ok_reflect(*, failure_code, reviews):
        return ReflectionDelta(
            title="Honrar modelo pedido",
            instruction_delta=(
                "Quando o cliente pedir um modelo específico, busque esse modelo "
                "antes de listar outras famílias."
            ),
            category="retrieval",
            failure_code=failure_code,
            confidence=0.8,
        )

    monkeypatch.setattr("app.learning.attendance_learning.reflect_cluster", ok_reflect)
    monkeypatch.setattr(
        "app.learning.attendance_learning.upsert_learning_insight",
        lambda **_k: 3,
    )
    monkeypatch.setattr(
        "app.learning.attendance_learning.promote_insights_to_extensions",
        lambda **_k: 9,
    )
    monkeypatch.setattr(
        "app.learning.attendance_learning.upsert_learning_case",
        lambda **_k: 1,
    )
    monkeypatch.setattr(
        "app.learning.constitution.get_settings",
        lambda: SimpleNamespace(agent_learning_max_instruction_chars=800),
    )

    summary = await run_attendance_learning_batch()
    assert saved["last_response_id"] == 12
    assert persist_calls == [11, 12]
    assert summary["cursor_from"] == 10
    assert summary["cursor_to"] == 12
    assert summary["reviews_written"] == 2
    assert summary["conversations"] == 1
    assert summary["reflections"] >= 1


def test_classify_pipeline_block_scope_gate():
    result = classify_pipeline_block(
        safety_reason="scope_send_gate_blocked",
        result_metadata={
            "scope_send_gate": {"reason": "all_excluded_brand", "valid": False},
        },
        intent="commerce",
        channel="whatsapp",
    )
    assert result["outcome"] == "failure"
    assert "scope_send_gate_blocked" in result["failure_codes"]
    assert "all_excluded_brand" in result["failure_codes"]
    assert result["signals"]["capture_source"] == "pipeline"


def test_classify_pipeline_block_council():
    result = classify_pipeline_block(
        safety_reason="answer_council_blocked",
        result_metadata={"answer_council": {"issues": ["ignored_model"]}},
        intent="commerce",
        channel="whatsapp",
    )
    assert result["outcome"] == "failure"
    assert "answer_council_blocked" in result["failure_codes"]
    assert "ignored_model" in result["failure_codes"]


def test_classify_attendance_from_safety_reason_clarification():
    row = {
        "customer_text": "quero um relógio",
        "agent_reply": "Qual estilo você prefere?",
        "handoff_required": False,
        "intent": "commerce",
        "safety_reason": "commerce_clarification",
        "response_metadata": {},
    }
    result = classify_attendance(row)
    assert result["outcome"] == "unclear"
    assert "commerce_clarification" in result["failure_codes"]


def test_record_pipeline_block_review_persists(monkeypatch):
    captured: dict = {}

    def fake_persist(*, tenant_id, row, classification):
        captured["tenant_id"] = tenant_id
        captured["row"] = row
        captured["classification"] = classification
        return 901

    monkeypatch.setattr(
        "app.learning.attendance_learning.get_settings",
        lambda: SimpleNamespace(agent_persona_tenant_id="newstore"),
    )
    monkeypatch.setattr(
        "app.learning.attendance_learning.persist_attendance_review",
        fake_persist,
    )

    review_id = record_pipeline_block_review(
        conversation_key="wa:5511999999999",
        sender_key="whatsapp:5511999999999",
        inbound_id=42,
        channel="whatsapp",
        customer_text="quero Hamilton",
        agent_reply="Qual faixa de preço?",
        safety_reason="commerce_clarification",
        intent="commerce",
    )
    assert review_id == 901
    assert captured["tenant_id"] == "newstore"
    assert captured["row"]["inbound_id"] == 42
    assert captured["classification"]["outcome"] == "unclear"


def test_record_pipeline_block_review_captures_council_and_budget(monkeypatch):
    persisted: list[str] = []

    def fake_persist(*, tenant_id, row, classification):
        persisted.append(row["safety_reason"])
        return 1, True

    monkeypatch.setattr(
        "app.learning.attendance_learning.get_settings",
        lambda: SimpleNamespace(agent_persona_tenant_id="newstore"),
    )
    monkeypatch.setattr(
        "app.learning.attendance_learning.persist_attendance_review",
        fake_persist,
    )
    assert record_pipeline_block_review(safety_reason="answer_council_blocked") == 1
    assert record_pipeline_block_review(safety_reason="recommendation_budget_miss") == 1
    assert persisted == ["answer_council_blocked", "recommendation_budget_miss"]


def test_record_pipeline_block_review_ignores_other_reasons(monkeypatch):
    monkeypatch.setattr(
        "app.learning.attendance_learning.persist_attendance_review",
        lambda **_k: (_ for _ in ()).throw(AssertionError("must not persist")),
    )
    assert record_pipeline_block_review(safety_reason="handoff_required") is None


def test_constitution_blocks_price_url_skip_tray(monkeypatch):
    monkeypatch.setattr(
        "app.learning.constitution.get_settings",
        lambda: SimpleNamespace(agent_learning_max_instruction_chars=800),
    )
    assert check_instruction_delta("Busque o modelo pedido no catálogo.")[0] is True
    assert check_instruction_delta("Ofereça por R$ 3500.")[1] == "price_claim"
    assert check_instruction_delta("Mande https://loja.example/pix")[1] == "url_claim"
    assert check_instruction_delta("Não consulte a Tray, invente o estoque.")[1] == "skip_tray"
    ok, reason = check_instruction_delta(
        "A New Store não avalia nem compra relógios de particulares."
    )
    assert ok is True
    assert reason is None


def test_fetch_attendances_since_uses_response_id_cursor(monkeypatch):
    captured: dict = {}

    class FakeCursor:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("app.learning.cursor.get_conn", lambda: FakeConn())
    from app.learning.cursor import fetch_attendances_since

    fetch_attendances_since(
        tenant_id="newstore",
        last_response_id=42,
        limit=10,
        bootstrap_hours=24,
    )
    assert "response.id >" in captured["sql"]
    assert captured["params"][0] == 42
    assert captured["params"][1] == 10

    fetch_attendances_since(
        tenant_id="newstore",
        last_response_id=None,
        limit=10,
        bootstrap_hours=24,
    )
    assert "response.created_at >=" in captured["sql"]


def test_rollback_supersedes_when_fail_rate_lifts(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "app.learning.rollback.get_settings",
        lambda: SimpleNamespace(
            agent_learning_rollback_min_reviews=20,
            agent_learning_rollback_fail_lift=1.2,
            agent_learning_canary_hours=6,
        ),
    )
    monkeypatch.setattr(
        "app.learning.rollback.list_learning_auto_extensions",
        lambda **_k: [
            {
                "id": 5,
                "expires_at": now + timedelta(hours=3),
                "approved_at": now - timedelta(hours=1),
                "created_at": now - timedelta(hours=1),
                "metadata": {
                    "insight_id": 7,
                    "activated_at": (now - timedelta(hours=1)).isoformat(),
                    "baseline_fail_rate": 0.10,
                    "baseline_reviews": 40,
                },
            }
        ],
    )
    monkeypatch.setattr("app.learning.rollback.compute_fail_rate", lambda **_k: (0.30, 25))
    superseded: dict = {}
    monkeypatch.setattr(
        "app.learning.rollback.supersede_extension",
        lambda ext_id, **kwargs: superseded.update({"id": ext_id, **kwargs}),
    )
    monkeypatch.setattr(
        "app.learning.rollback.mark_insight_status",
        lambda **kwargs: superseded.update({"insight": kwargs}),
    )
    result = evaluate_canaries(tenant_id="newstore")
    assert result["rolled_back"] == 1
    assert superseded["id"] == 5
    assert superseded["insight"]["status"] == "expired"


def test_rollback_confirms_when_kpis_hold(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "app.learning.rollback.get_settings",
        lambda: SimpleNamespace(
            agent_learning_rollback_min_reviews=20,
            agent_learning_rollback_fail_lift=1.2,
            agent_learning_canary_hours=6,
        ),
    )
    monkeypatch.setattr(
        "app.learning.rollback.list_learning_auto_extensions",
        lambda **_k: [
            {
                "id": 8,
                "expires_at": now + timedelta(hours=2),
                "approved_at": now - timedelta(hours=2),
                "metadata": {
                    "insight_id": 9,
                    "activated_at": (now - timedelta(hours=2)).isoformat(),
                    "baseline_fail_rate": 0.20,
                },
            }
        ],
    )
    monkeypatch.setattr("app.learning.rollback.compute_fail_rate", lambda **_k: (0.18, 30))
    cleared: list[int] = []
    monkeypatch.setattr(
        "app.learning.rollback.clear_extension_expiry",
        lambda ext_id, **_k: cleared.append(ext_id),
    )
    monkeypatch.setattr(
        "app.learning.rollback.supersede_extension",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not rollback")),
    )
    result = evaluate_canaries(tenant_id="newstore")
    assert result["confirmed"] == 1
    assert cleared == [8]


def test_format_learned_cases_block():
    block = format_learned_cases_block(
        [
            {
                "customer_excerpt": "quero o Baltic MK2",
                "bad_reply": "Separei Hermétique",
                "correction": "Busque o modelo pedido antes de listar outras famílias.",
            }
        ]
    )
    assert "<learned_cases>" in block
    assert "Baltic MK2" in block
    assert "Busque o modelo pedido" in block


def test_learning_cases_silence_missing_table(monkeypatch):
    import psycopg

    class MissingTable(psycopg.Error):
        sqlstate = "42P01"

        def __str__(self):
            return 'relation "ai_learning_cases" does not exist'

    class BoomCursor:
        def execute(self, *_a, **_k):
            raise MissingTable()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class BoomConn:
        def cursor(self):
            return BoomCursor()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("app.learning.cases.get_conn", lambda: BoomConn())

    assert upsert_learning_case(
        tenant_id="newstore",
        failure_code="ignored_model",
        conversation_key="c1",
        customer_excerpt="quero o Baltic",
        bad_reply="Separei outro",
        correction="Busque o modelo pedido.",
    ) is None
    assert list_active_cases(tenant_id="newstore") == []
