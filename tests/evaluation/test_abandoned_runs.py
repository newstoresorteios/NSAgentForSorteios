from contextlib import contextmanager
from unittest.mock import MagicMock
import pytest
from app.evaluation import regression_repository as repository


def connection_for(monkeypatch, cursor):
    cursor.__enter__.return_value = cursor
    connection = MagicMock()
    connection.cursor.return_value = cursor
    @contextmanager
    def connect():
        yield connection
    monkeypatch.setattr(repository, 'get_conn', connect)


def test_terminal_attempt_cannot_be_reclaimed_or_spend_again(monkeypatch):
    cursor = MagicMock()
    cursor.fetchone.return_value = {'suite_id': 's', 'scenario_key': 'case', 'next_step': 0,
                                  'active_step': None, 'status': 'completed', 'versions': {}}
    connection_for(monkeypatch, cursor)
    with pytest.raises(ValueError, match='terminal'):
        repository.claim_turn('w', 's', 'case', 'r', 0, {})
    assert not any('SET active_step=' in args.args[0] for args in cursor.execute.call_args_list)


def test_expiry_scopes_workspace_preserves_inconclusive_and_never_retries(monkeypatch):
    cursor = MagicMock()
    cursor.fetchall.side_effect = [[{'id': 'reg'}], [{'id': 'history'}]]
    connection_for(monkeypatch, cursor)
    result = repository.expire_stale_runs('workspace', min_age_seconds=1)
    assert result['minimum_age_seconds'] == 900
    assert result['automatic_retry'] is False and result['outcome'] == 'inconclusive'
    assert result['regression_runs'] == ['reg'] and result['historical_runs'] == ['history']
    for call in cursor.execute.call_args_list:
        sql, params = call.args
        assert params == ('workspace', 900)
        assert "status='running'" in sql and 'make_interval' in sql


def test_late_worker_cannot_complete_a_terminated_claim(monkeypatch):
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    connection_for(monkeypatch, cursor)
    with pytest.raises(ValueError, match='claim_lost'):
        repository.finish_turn('w', 'r', 0, {}, {}, {}, True)
    assert "status='running'" in cursor.execute.call_args.args[0]
