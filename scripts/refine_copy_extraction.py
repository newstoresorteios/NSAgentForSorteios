"""Refine the one-time extraction without touching unrelated edits."""
import ast
import hashlib
import json
import subprocess
from migrate_operator_copy import ROOT, SEED, apply_edits, template_of

fields = json.loads(SEED.read_text(encoding="utf-8"))
generated = {f["key"][8:] for f in fields if f.get("source")}
updates, missing = [], []
for path in sorted((ROOT / "app").rglob("*.py")):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "operator_message" and n.args and isinstance(n.args[0], ast.Constant)
             and n.args[0].value in generated]
    if not calls:
        continue
    original = subprocess.run(["git", "show", "HEAD:" + path.relative_to(ROOT).as_posix()], cwd=ROOT,
                              check=True, capture_output=True).stdout.decode("utf-8")
    original_tree = ast.parse(original)
    parents = {child: node for node in ast.walk(original_tree) for child in ast.iter_child_nodes(node)}
    originals = {}
    for n in ast.walk(original_tree):
        if not isinstance(n, (ast.Constant, ast.JoinedStr)):
            continue
        parsed = template_of(n)
        if not parsed:
            continue
        ancestor = parents.get(n)
        while ancestor and not isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ancestor = parents.get(ancestor)
        if not ancestor:
            continue
        prefix = ".".join(path.relative_to(ROOT / "app").with_suffix("").parts) + "." + ancestor.name
        key = prefix + "." + hashlib.sha256(parsed[0].encode()).hexdigest()[:10]
        originals[key] = ast.get_source_segment(original, n)
    edits = []
    for call in calls:
        key = call.args[0].value
        if key not in originals:
            missing.append(key)
        else:
            edits.append((call, originals[key]))
    updates.append((path, edits))
if missing:
    raise RuntimeError(str(missing))
for path, edits in updates:
    apply_edits(path, edits)
fields = [f for f in fields if not f.get("source")]
SEED.write_text(json.dumps(fields, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
print("Restored generated expressions; unrelated edits preserved.")
