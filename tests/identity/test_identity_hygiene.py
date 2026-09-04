import ast
from pathlib import Path


def test_identity_exception_handlers_are_not_bare_pass():
    silent: list[str] = []
    for path in Path("app/identity").rglob("*.py"):
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
