from app.core.turn_cache import begin_turn_cache, end_turn_cache, cached_turn_read


def test_reads_are_reused_only_in_same_turn_and_scope():
    calls = []
    @cached_turn_read
    def read(workspace):
        calls.append(workspace)
        return {"items": [workspace]}
    token = begin_turn_cache()
    try:
        first = read("a")
        first["items"].clear()
        assert read("a") == {"items": ["a"]}
        assert read("b") == {"items": ["b"]}
        assert calls == ["a", "b"]
    finally:
        end_turn_cache(token)
    assert read("a") == {"items": ["a"]}
    assert calls == ["a", "b", "a"]
