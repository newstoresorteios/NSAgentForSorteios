"""Guard: compiled retrieval is the ranking authority; catalog_ranking is leftover API."""

from __future__ import annotations

import ast
from pathlib import Path

from app.catalog.index.catalog_index import hybrid_rank_products
from app.models import SalesInterpretation
from app.sales.workflows.catalog_ranking import rank_candidates

_ROOT = Path("app/catalog")


def _assert_no_sales_imports(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app.sales"), f"{path}: {alias.name}"
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("app.sales"), f"{path}: {node.module}"
            assert node.module != "app.sales_agent", f"{path}: {node.module}"


def test_catalog_does_not_import_sales() -> None:
    paths = [
        _ROOT / "retrieval" / "executor.py",
        _ROOT / "retrieval" / "ports.py",
        _ROOT / "specs" / "preference_normalize.py",
        _ROOT / "specs" / "identity_lock.py",
        _ROOT / "vision" / "image_product_id.py",
        _ROOT / "vision" / "prompt.py",
    ]
    for path in paths:
        assert path.is_file(), path
        _assert_no_sales_imports(path)


def test_executor_does_not_import_sales() -> None:
    _assert_no_sales_imports(_ROOT / "retrieval" / "executor.py")


def test_rank_authority_module_does_not_import_sales() -> None:
    _assert_no_sales_imports(_ROOT / "retrieval" / "rank_authority.py")


def test_recommendation_uses_hybrid_rank_not_catalog_ranking() -> None:
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Omega", "product_type": "relógio"},
        preferences={"budget_max": 8000},
        information_needed=["catalog"],
        references_previous_context=True,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
    )
    products = [
        {
            "id": "1",
            "name": "Omega Seamaster Diver 300M",
            "brand": "Omega",
            "current_price": 7200,
        },
        {
            "id": "2",
            "name": "Omega Speedmaster",
            "brand": "Omega",
            "current_price": 9500,
        },
    ]
    hybrid = hybrid_rank_products(products, interpretation, mode="recommendation")
    leftover = rank_candidates(
        products,
        {"subject": {"brand": "Omega"}, "constraints": {"budget_max": 8000}},
    )
    assert hybrid[0]["id"] == "1"
    assert leftover[0]["id"] == "1"
