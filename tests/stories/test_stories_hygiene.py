import ast
from pathlib import Path


def test_stories_exception_handlers_are_not_bare_pass():
    silent: list[str] = []
    for path in Path("app/stories").rglob("*.py"):
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


def test_has_recoverable_story_context_is_gone():
    source = Path("app/stories/instagram_story_intent.py").read_text(encoding="utf-8")
    assert "def has_recoverable_story_context" not in source


def test_log_swallowed_prints_error_type(capsys):
    from app.stories import log_swallowed

    log_swallowed("matcher.index_best_effort", RuntimeError("boom"))
    output = capsys.readouterr().out
    assert "[stories.matcher.index_best_effort]" in output
    assert "RuntimeError" in output
