import ast
from pathlib import Path


def test_ops_exception_handlers_are_not_bare_pass():
    silent: list[str] = []
    for path in Path("app/ops").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = ast.unparse(node.type) if node.type is not None else "BaseException"
            if caught not in {"Exception", "BaseException"}:
                continue
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                silent.append(f"{path.as_posix()}:{node.lineno}")
    assert silent == []


def test_cleanup_stale_takeover_state_defined_once():
    tree = ast.parse(Path("app/ops/human_takeover.py").read_text(encoding="utf-8"))
    defs = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "cleanup_stale_takeover_state"
    ]
    assert defs == ["cleanup_stale_takeover_state"]
