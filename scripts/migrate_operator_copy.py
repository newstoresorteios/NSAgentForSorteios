"""One-time, source-preserving extraction of operator-authored response copy.

Review the diff and catalog before publishing. Runtime uses only database values.
The source locations and stable keys let operators identify each fallback.
"""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "sql/seeds/operator_catalog.json"


def apply_edits(path, edits):
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    replacements = []
    for node, replacement in edits:
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        replacements.append((start, end, replacement.encode()))
    for start, end, replacement in sorted(replacements, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    path.write_bytes(raw)


def template_of(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.replace("{", "{{").replace("}", "}}"), []
    if not isinstance(node, ast.JoinedStr):
        return None
    text, args = "", []
    for value in node.values:
        if isinstance(value, ast.Constant):
            text += value.value.replace("{", "{{").replace("}", "}}")
        else:
            key = value.value.id if isinstance(value.value, ast.Name) else f"value_{len(args) + 1}"
            if key in {k for k, _ in args}:
                key = f"value_{len(args) + 1}"
            # Evaluate formatting in trusted Python, never operator templates.
            expression = ast.unparse(ast.JoinedStr(values=[value]))
            text += "{" + key + "}"
            args.append((key, expression))
    return text, args


def add_field(fields, key, template, source, group="Mensagens de atendimento"):
    if any(f["key"] == "message." + key for f in fields):
        return
    fields.append({"key": "message." + key, "target": "message", "attribute": key,
        "default": template, "label": template[:95].replace("\n", " "),
        "description": f"Texto operacional em {source}. Preserve as variáveis entre chaves.",
        "source": source, "group": group, "type": "textarea", "maxLength": max(6000, len(template)*2)})


def call_for(fields, node, source, prefix):
    parsed = template_of(node)
    if parsed is None:
        return None
    template, args = parsed
    digest = hashlib.sha256(template.encode()).hexdigest()[:10]
    key = f"{prefix}.{digest}"
    add_field(fields, key, template, source)
    arguments = ", ".join(f"{key}={value}" for key, value in args)
    return f"operator_message({key!r}" + (", " + arguments if arguments else "") + ")"


def add_import(path):
    source = path.read_text(encoding="utf-8")
    if "import message as operator_message" in source:
        return
    tree = ast.parse(source)
    anchor = next((n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__"), None)
    if anchor is None and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        anchor = tree.body[0]
    lines = source.splitlines(keepends=True)
    lines.insert(anchor.end_lineno if anchor else 0, "\nfrom app.configuration.runtime import message as operator_message\n")
    path.write_text("".join(lines), encoding="utf-8")


def main():
    fields = json.loads(SEED.read_text(encoding="utf-8"))
    migrated = 0
    folders = ("commerce", "sales", "agents", "identity", "verify", "llm", "ops", "persona")
    for path in sorted((ROOT / "app").rglob("*.py")):
        if not any(part in folders for part in path.parts) and path.name != "sales_agent.py":
            continue
        if path.name in {"site_knowledge.py", "store_knowledge.py", "persona_policy.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        edits = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Constant, ast.JoinedStr)):
                continue
            if isinstance(node, ast.Constant) and not isinstance(node.value, str):
                continue
            parent = parents.get(node)
            if isinstance(parent, (ast.JoinedStr, ast.FormattedValue)):
                continue
            ancestry = []
            ancestor = parent
            while ancestor:
                ancestry.append(ancestor)
                ancestor = parents.get(ancestor)
            function = next((a for a in ancestry if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
            if function is None:
                continue
            # Exclude docstrings, diagnostic output, regexes and matching logic.
            if isinstance(parent, ast.Expr) or any(isinstance(a, (ast.Compare, ast.Match, ast.FormattedValue, ast.JoinedStr)) for a in ancestry):
                continue
            if any(isinstance(a, ast.Call) and (ast.unparse(a.func).startswith(("log", "print", "re.", "logger.", "operator_message"))) for a in ancestry):
                continue
            direct_reply = any(isinstance(a, ast.keyword) and a.arg in {"reply_text", "better_reply_hint", "instructions", "system_prompt"} for a in ancestry)
            copy_function = any(word in function.name for word in ("reply", "greeting", "template", "copy", "fallback", "prompt"))
            copy_assignment = any(isinstance(a, (ast.Assign, ast.AnnAssign)) and any(
                isinstance(t, ast.Name) and t.id in {"reply", "reply_text", "fallback", "hint", "question"}
                for t in (a.targets if isinstance(a, ast.Assign) else [a.target])) for a in ancestry)
            if not (direct_reply or copy_function or copy_assignment):
                continue
            parsed = template_of(node)
            if not parsed:
                continue
            literal = parsed[0]
            import re
            prose = re.sub(r"\{[^}]+\}", "", literal)
            if len(prose.split()) < 3 or "\\" in literal:
                continue
            if any(node in list(ast.walk(d)) for d in function.decorator_list):
                continue
            prefix = ".".join(path.relative_to(ROOT / "app").with_suffix("").parts) + "." + function.name
            replacement = call_for(fields, node, str(path.relative_to(ROOT)) + ":" + str(node.lineno), prefix)
            edits.append((node, replacement))
        if edits:
            apply_edits(path, edits)
            add_import(path)
            migrated += len(edits)
    SEED.write_text(json.dumps(fields, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
    print(json.dumps({"migrated": migrated, "catalog_fields": len(fields)}))


if __name__ == "__main__":
    main()
