from __future__ import annotations

import unicodedata
from typing import Any


def fold_text(value: Any) -> str:
    text = str(value or "")
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text).lower()
        if not unicodedata.combining(char)
    ).strip()


# Historical name used across retrieval modules.
_fold = fold_text


def product_text(product: dict[str, Any]) -> str:
    fields = (
        "name", "brand", "model", "reference", "ean", "description",
        "category", "category_name", "category_id", "attributes", "color",
        "style", "material", "properties", "ProductSettings", "variants",
        "case_size", "water_resistance_m", "mechanism",
    )
    chunks = [str(product.get(field) or "") for field in fields]
    wr = product.get("water_resistance_m")
    if wr is not None and str(wr).strip():
        chunks.append(f"{wr}m")
    case = product.get("case_size")
    if case is not None and str(case).strip() and "mm" not in str(case).lower():
        chunks.append(f"{case}mm")
    return fold_text(" ".join(chunks))


_product_text = product_text
