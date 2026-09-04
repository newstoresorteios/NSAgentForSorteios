import ast
from pathlib import Path


def _bare_pass_handlers(path: Path) -> list[str]:
    silent: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        caught = ast.unparse(node.type) if node.type is not None else "BaseException"
        if caught not in {"Exception", "BaseException"}:
            continue
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            silent.append(f"{path.as_posix()}:{node.lineno}")
    return silent


def test_sales_exception_handlers_are_not_bare_pass():
    silent: list[str] = []
    for path in Path("app/sales").rglob("*.py"):
        silent.extend(_bare_pass_handlers(path))
    silent.extend(_bare_pass_handlers(Path("app/sales_agent.py")))
    assert silent == []


def test_plausible_name_logs_swallowed_nick_check(monkeypatch, capsys):
    from app.sales.qualification_slots import _is_plausible_name

    def _boom(_text: str) -> bool:
        raise RuntimeError("nick-fail")

    monkeypatch.setattr(
        "app.identity.identity_names.looks_like_whatsapp_nick",
        _boom,
    )
    assert _is_plausible_name("Maria") is True
    output = capsys.readouterr().out
    assert "[sales.qualification.whatsapp_nick]" in output
    assert "RuntimeError" in output
