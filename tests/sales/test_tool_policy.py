from app.sales.policies.tool_policy import apply_tool_policy, evaluate_tool_policy


def test_create_cart_without_product_would_block():
    verdict = evaluate_tool_policy("create_cart", {"quantity": 1})
    assert verdict["action"] == "block"
    assert "create_cart_missing_product" in verdict["reasons"]


def test_create_cart_with_product_is_allowed():
    verdict = evaluate_tool_policy(
        "create_cart",
        {"product_id": "10", "quantity": 1, "session_id": "s1"},
    )
    assert verdict["action"] == "allow"


def test_search_is_not_a_mutating_tool(monkeypatch):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_TOOL_POLICY_MODE", "enforce")
    get_settings.cache_clear()
    try:
        assert apply_tool_policy("search_products", {"query": "seiko"}) is None
    finally:
        get_settings.cache_clear()


def test_shadow_does_not_block_bad_create_cart(monkeypatch, capsys):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_TOOL_POLICY_MODE", "shadow")
    get_settings.cache_clear()
    try:
        blocked = apply_tool_policy("create_cart", {"quantity": 1})
    finally:
        get_settings.cache_clear()
    assert blocked is None
    output = capsys.readouterr().out
    assert "[sales.tool.policy]" in output
    assert "would_block" in output


def test_enforce_blocks_empty_create_order(monkeypatch):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_TOOL_POLICY_MODE", "enforce")
    get_settings.cache_clear()
    try:
        blocked = apply_tool_policy("create_order", {})
    finally:
        get_settings.cache_clear()
    assert blocked is not None
    assert blocked["error"] == "tool_policy_blocked"
    assert "create_order_empty_payload" in blocked["policy_reasons"]
