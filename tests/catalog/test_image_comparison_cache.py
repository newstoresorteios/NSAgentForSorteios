from app.catalog.media import storefront_search as images
from app.ops.turn_runtime import TurnRuntimeContext
from app.ops.runtime_context import set_current_turn,reset_current_turn

def test_cache_is_byte_and_turn_scoped(monkeypatch):
    calls=[]
    def compare(a,b):
        calls.append((a,b));return (0,.01,'source','candidate')
    monkeypatch.setattr(images,'best_image_view_metrics',compare)
    token=set_current_turn(TurnRuntimeContext(trace_id='test'))
    try:
        images.cached_image_view_metrics(b'a',b'b')
        images.cached_image_view_metrics(b'a',b'b')
        images.cached_image_view_metrics(b'crop',b'b')
        assert len(calls)==2
    finally:reset_current_turn(token)
    images.cached_image_view_metrics(b'a',b'b')
    assert len(calls)==3
