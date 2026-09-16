from __future__ import annotations

from copy import deepcopy
import json
import time


# Capability boundary, not a business policy. Operators cannot enable writes.
READ_TOOLS = frozenset({"search_products", "get_product", "get_product_link", "check_inventory",
                       "list_categories", "get_category", "get_category_tree", "list_product_variants", "get_product_variant"})


async def evaluate_tool(context, name, arguments, execute):
    key = json.dumps([name, arguments], sort_keys=True, ensure_ascii=False)
    started = time.perf_counter()
    context.started_tool_calls += 1
    if name not in READ_TOOLS or context.started_tool_calls > context.max_tool_calls:
        context.blocked.append(name)
        result = {"error": "evaluation_tool_prohibited", "tool": name}
    elif context.fixtures is not None:
        result = deepcopy(context.fixtures.get(key))
        if result is None:
            context.blocked.append("fixture_missing:" + name)
            result = {"error": "evaluation_fixture_missing", "tool": name}
        from app.ops.observability import record_tray_observation
        record_tray_observation(tool=name, arguments=arguments, result=result,
                                elapsed_ms=(time.perf_counter() - started) * 1000)
    else:
        result = await execute()
        context.captured[key] = deepcopy(result)
    context.tool_calls.append({"tool": name, "arguments": deepcopy(arguments), "result": deepcopy(result),
                               "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
    return result
