from contextlib import contextmanager

from app.ops.instagram_profile_backfill import backfill_instagram_profile_pictures


class _Cursor:
    def __init__(self, candidates):
        self.candidates = candidates
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.queries.append((" ".join(query.split()), params))

    def fetchall(self):
        return self.candidates


class _Connection:
    def __init__(self, candidates):
        self.cursor_instance = _Cursor(candidates)
        self.committed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True


def test_backfill_updates_inbound_and_central_conversation(monkeypatch):
    candidate = {
        "id": 1029,
        "conversation_id": "ig:1052648647582097",
        "workspace_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "sender_external_id": "1052648647582097",
        "visitor_id": "1052648647582097",
        "sender_username": "cliente",
    }
    connections = [_Connection([candidate]), _Connection([])]

    @contextmanager
    def fake_conn():
        yield connections.pop(0)

    monkeypatch.setattr("app.db.get_conn", fake_conn)
    monkeypatch.setattr(
        "app.ops.instagram_profile_backfill._lookup_ig_profile",
        lambda _sender_id, force=False: {
            "profile_picture_url": "https://cdn.example/avatar.jpg",
            "profile_url": "https://www.instagram.com/cliente/",
        },
    )
    monkeypatch.setattr(
        "app.ops.instagram_profile_backfill.log_event", lambda *_args: None,
    )

    result = backfill_instagram_profile_pictures(limit=10)

    assert result == {
        "ok": True,
        "scanned": 1,
        "updated": 1,
        "unavailable": 0,
        "failed": 0,
    }
