from app.learning import cases
from app.learning.diagnose import classify_attendance
from tests.memory.test_workspace_memory_boundary import Cursor, connection, WORKSPACE


def test_retry_preserves_original_incident_and_different_turns_get_distinct_keys(monkeypatch):
    cursor = Cursor([{"id": 1}, None, {"id": 1}, {"id": 2}])
    monkeypatch.setattr(cases, "get_conn", connection(cursor))
    args = dict(tenant_id="store", workspace_id=WORKSPACE, failure_code="repeat_question",
                conversation_key="thread", customer_excerpt="Já falei", bad_reply="Qual seu orçamento?",
                correction="Use o orçamento já informado", source_response_id=10, source_review_ids=[90])
    assert cases.upsert_learning_case(**args) == 1
    assert cases.upsert_learning_case(**{**args, "correction": "Correção posterior"}) == 1
    assert cases.upsert_learning_case(**{**args, "source_response_id": 11}) == 2
    inserts = [(sql, params) for sql, params in cursor.calls if "INSERT INTO" in sql]
    assert all("DO NOTHING" in sql and "DO UPDATE" not in sql for sql, _ in inserts)
    assert inserts[0][1][2] == inserts[1][1][2]
    assert inserts[0][1][2] != inserts[2][1][2]
    assert inserts[0][1][-1].obj["pattern_key"] == "repeat_question"
    assert inserts[0][1][-1].obj["source_review_ids"] == [90]


def test_recent_cluster_reviews_preserve_recorded_turn_identity(monkeypatch):
    from types import SimpleNamespace
    from app.learning import attendance_learning as learning
    from contextlib import contextmanager
    statements = []

    class ReviewCursor:
        def execute(self, sql, params):
            statements.append(sql)

        def fetchall(self):
            return [{"id": 90, "conversation_key": "thread", "customer_text": "Já falei",
                     "agent_reply": "Qual seu orçamento?", "outcome": "failure",
                     "failure_codes": ["repeat_question"], "workspace_id": WORKSPACE,
                     "inbound_id": 5, "response_id": 10}]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    @contextmanager
    def conn():
        yield SimpleNamespace(cursor=ReviewCursor)

    monkeypatch.setattr(learning, "get_settings", lambda: SimpleNamespace(database_url="test"))
    monkeypatch.setattr(learning, "get_conn", conn)
    rows = learning.fetch_recent_reviews_for_cluster(tenant_id="store", workspace_id=WORKSPACE)
    assert rows[0]["inbound_id"] == 5 and rows[0]["response_id"] == 10
    assert "inbound_id, response_id" in statements[0]


def test_successful_closing_is_not_a_failed_clarification():
    result = classify_attendance({"customer_text": "Era isso, obrigado!", "agent_reply": "Disponha!",
                                  "safety_reason": "commerce_clarification"})
    assert result["outcome"] == "success"
    assert result["failure_codes"] == []


def test_repeated_question_after_thanks_is_still_flagged():
    result = classify_attendance({"customer_text": "Obrigado", "agent_reply": "Qual seu orçamento?",
                                  "safety_reason": "commerce_clarification"})
    assert result["outcome"] == "unclear"
    assert result["failure_codes"] == ["commerce_clarification"]
