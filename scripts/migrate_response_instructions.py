"""Extract commercial response instructions, retaining lazy workspace evaluation."""
import ast
import copy
import json
from collections import Counter
from migrate_operator_copy import ROOT, SEED, apply_edits, add_import, call_for

fields = json.loads(SEED.read_text(encoding="utf-8"))
targets = {
    "app/sales_agent.py": {"SALES_PLANNER_INSTRUCTIONS", "SALES_RESPONDER_INSTRUCTIONS", "BASE_SALES_RESPONDER_INSTRUCTIONS", "SALES_CLARIFICATION_INSTRUCTIONS", "OUT_OF_SCOPE_REPLY", "SALES_INTERPRETER_INSTRUCTIONS", "CHECKOUT_FLOW_INSTRUCTIONS"},
    "app/channels/audio_service.py": {"AUDIO_TRANSCRIPTION_FAILED_REPLY", "_WHISPER_BRAND_PROMPT"},
    "app/channels/brevo_client.py": {"_EMPTY_REPLY_FALLBACK"},
    "app/channels/brevo_instagram_media.py": {"UNVIEWABLE_MEDIA_GUIDE_REPLY", "PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY"},
    "app/commerce/commerce_router.py": {"COMMERCE_UNAVAILABLE", "PRODUCT_NOT_FOUND_REPLY"},
    "app/commerce/shipping_service.py": {"SHIPPING_LEADTIME_GUIDANCE"},
    "app/sales/scope_send_gate.py": {"SCOPE_SEND_FALLBACK", "PURCHASE_CLOSE_RELIST_FALLBACK"},
}
all_names = set.union(*targets.values())
for relative, names in targets.items():
    path = ROOT / relative
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assignments = [n for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in names]
    counts = Counter(n.targets[0].id for n in assignments)
    occurrences, previous, edits = Counter(), {}, []
    for node in assignments:
        name = node.targets[0].id
        occurrences[name] += 1
        function_name = name if occurrences[name] == counts[name] else "_operator_"+name.lower()+"_"+str(occurrences[name])
        class References(ast.NodeTransformer):
            def visit_Name(self, n):
                if isinstance(n.ctx, ast.Load) and n.id in names:
                    return ast.copy_location(ast.Call(func=ast.Name(id=previous.get(n.id,n.id),ctx=ast.Load()),args=[],keywords=[]), n)
                return n
        value = References().visit(copy.deepcopy(node.value))
        stripped = isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "strip"
        if stripped:
            value = value.func.value
        body = call_for(fields, value, relative+":"+str(node.lineno), "instructions."+function_name.lower())
        if body is None:
            body = ast.unparse(value)  # versioned alias, no new text
        if stripped:
            body += ".strip()"
        edits.append((node, f"def {function_name}():\n    return {body}"))
        previous[name] = function_name
    apply_edits(path, edits)
    add_import(path)

for folder in ("app","tests"):
    for path in (ROOT / folder).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child:node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        edits = []
        for node in ast.walk(tree):
            match = isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in all_names
            match |= isinstance(node, ast.Attribute) and node.attr in all_names
            if not match:
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Call) and parent.func is node:
                continue
            edits.append((node, ast.unparse(node)+"()"))
        if edits:
            apply_edits(path, edits)
SEED.write_text(json.dumps(fields,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
print(json.dumps({"response_constants":len(all_names),"catalog_fields":len(fields)}))
