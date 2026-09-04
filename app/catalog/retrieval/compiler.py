from __future__ import annotations

import re
from typing import Any

from app.models import SalesInterpretation
from app.catalog.retrieval.limits import (
    CATALOG_DISCOVERY_MAX_PAGES,
    CATALOG_DISCOVERY_MAX_PRODUCTS,
    PRODUCT_PAGE_LIMIT,
)
from app.catalog.retrieval.text import _fold
from app.catalog.retrieval.tokens import (
    catalog_match_tokens,
    commercial_model_aliases,
    effective_product_reference,
    extract_model_codes,
    extract_reference_code,
    identity_core_tokens,
    normalize_pt_catalog_query,
    preference_color_search_labels,
    preference_color_tokens,
    preference_gender_tokens,
)
from app.catalog.retrieval.types import ProductRetrievalPlan, ProductRetrievalRequest

class ProductRetrievalCompiler:
    @staticmethod
    def compile(
        interpretation: SalesInterpretation,
        *,
        category_ids: tuple[str, ...] | list[str] = (),
    ) -> ProductRetrievalPlan:
        subject = interpretation.subject
        from app.catalog.specs.preference_normalize import is_gender_only_label

        model_for_exact = None if is_gender_only_label(subject.model) else subject.model
        inferred_reference = effective_product_reference(subject.reference) or (
            extract_reference_code(
                " ".join(
                    part
                    for part in (model_for_exact, subject.brand, subject.product_type)
                    if part
                )
            )
        )
        exact = bool(inferred_reference or subject.ean or model_for_exact)
        if bool(getattr(interpretation, "_force_recommendation_mode", False)):
            exact = False
        requests: list[ProductRetrievalRequest] = []

        if subject.ean:
            requests.append(ProductRetrievalRequest(strategy="exact_ean", ean=subject.ean))
        elif inferred_reference:
            requests.append(
                ProductRetrievalRequest(
                    strategy="exact_reference",
                    reference=inferred_reference,
                )
            )
            if model_for_exact:
                requests.append(
                    ProductRetrievalRequest(
                        strategy="exact_query_reference_context",
                        query=" ".join(
                            part
                            for part in (subject.brand, model_for_exact)
                            if part
                        ).strip(),
                    )
                )
        elif model_for_exact:
            pt_model = normalize_pt_catalog_query(model_for_exact)
            color_tokens = preference_color_tokens(interpretation)
            color_label = " ".join(color_tokens).strip()
            core_tokens = identity_core_tokens(
                pt_model or subject.model,
                color_tokens=color_tokens,
            )
            core_query = " ".join(core_tokens[:4]).strip()
            core_label = core_query.title() if core_query else ""
            model_codes = extract_model_codes(pt_model or subject.model)
            wants_automatic = bool(
                re.search(r"\b(automatic|automatico)\b", _fold(subject.model))
            )
            auto_bit = "Automático" if wants_automatic else None
            color_hue = color_label.title() if color_label else None
            seen_probe_keys: set[str] = set()
            # Extra slot when dial color is known so short family+color and the
            # catalog-title probe can both run without dropping brand query.
            tier1_budget = 7 if color_hue else 6
            match_tokens = catalog_match_tokens(interpretation)
            if match_tokens:
                requests.append(
                    ProductRetrievalRequest(
                        strategy="token_and_search",
                        brand=subject.brand,
                        tokens=match_tokens,
                        limit=PRODUCT_PAGE_LIMIT,
                    )
                )
                # Soften AND when Vision invents a rare sibling name (Navihawk)
                # or piles color+movement onto an already long identity.
                color_set = set(preference_color_tokens(interpretation))
                without_color = tuple(
                    token for token in match_tokens if token not in color_set
                )
                if without_color and without_color != match_tokens:
                    requests.append(
                        ProductRetrievalRequest(
                            strategy="token_and_search_no_color",
                            brand=subject.brand,
                            tokens=without_color,
                            limit=PRODUCT_PAGE_LIMIT,
                        )
                    )
                # Brand + first few identity tokens (sky/pilot/promaster…).
                brand_fold = _fold(subject.brand)
                core_only = tuple(
                    token
                    for token in without_color or match_tokens
                    if token != brand_fold
                )[:4]
                if subject.brand and core_only:
                    short = (brand_fold, *core_only) if brand_fold else core_only
                    short = tuple(dict.fromkeys(t for t in short if t))
                    if short != match_tokens and short != without_color:
                        requests.append(
                            ProductRetrievalRequest(
                                strategy="token_and_search_short",
                                brand=subject.brand,
                                tokens=short,
                                limit=PRODUCT_PAGE_LIMIT,
                            )
                        )

            def _add_probe(
                strategy: str,
                *,
                name: str | None = None,
                brand: str | None = None,
                query: str | None = None,
            ) -> None:
                nonlocal tier1_budget
                if tier1_budget <= 0:
                    return
                cleaned_name = " ".join(str(name or "").split()).strip() or None
                cleaned_query = " ".join(str(query or "").split()).strip() or None
                if not cleaned_name and not cleaned_query:
                    return
                # Phrase already contains the brand → do not also filter by brand.
                brand_filter = brand
                if (
                    brand_filter
                    and cleaned_name
                    and _fold(brand_filter) in _fold(cleaned_name)
                ):
                    brand_filter = None
                key = (
                    f"name|{_fold(cleaned_name)}|"
                    f"query|{_fold(cleaned_query)}|"
                    f"brand|{_fold(brand_filter)}"
                )
                if key in seen_probe_keys:
                    return
                seen_probe_keys.add(key)
                requests.append(
                    ProductRetrievalRequest(
                        strategy=strategy,
                        name=cleaned_name,
                        brand=brand_filter,
                        query=cleaned_query,
                    )
                )
                tier1_budget -= 1

            # Tier 1 — at most 6 high-signal probes (no brand paging here).
            if model_codes:
                _add_probe(
                    "exact_model_code",
                    name=model_codes[0],
                    brand=subject.brand,
                )
            identity_name = (
                " ".join(
                    part
                    for part in (core_label or None, auto_bit if color_hue else None)
                    if part
                ).strip()
                or (pt_model or subject.model)
            )
            _add_probe(
                "exact_model_with_brand" if subject.brand else "exact_model",
                name=identity_name if color_hue else (pt_model or subject.model),
                brand=subject.brand,
            )
            if color_hue and core_label:
                _add_probe(
                    "exact_color_core",
                    name=f"{core_label} {color_hue}".strip(),
                    brand=subject.brand,
                )
                if auto_bit:
                    _add_probe(
                        "exact_color_automatic",
                        name=f"{core_label} {auto_bit} {color_hue}".strip(),
                        brand=subject.brand,
                    )
            # Short family+color beats long titles on Tray's name filter.
            if color_hue and model_codes:
                _add_probe(
                    "exact_color_family_code",
                    name=f"{model_codes[0]} {color_hue}".strip(),
                    brand=subject.brand,
                )
            catalog_title = " ".join(
                part
                for part in (
                    "Relógio",
                    subject.brand,
                    core_label or None,
                    auto_bit,
                    color_hue,
                )
                if part
            ).strip()
            if catalog_title:
                _add_probe("exact_catalog_title", name=catalog_title)
            brand_model_query = " ".join(
                part for part in (subject.brand, core_label or identity_name) if part
            ).strip()
            if brand_model_query and _fold(brand_model_query) != _fold(
                identity_name or ""
            ):
                _add_probe("exact_query_full", query=brand_model_query)
            # Dial "Prospex Diver's 200m" → Tray titles Sea Samurai / King Turtle.
            for alias in commercial_model_aliases(
                pt_model or subject.model,
                brand=subject.brand,
            ):
                _add_probe(
                    "exact_commercial_alias",
                    name=alias,
                    brand=subject.brand,
                )
                if color_hue:
                    _add_probe(
                        "exact_commercial_alias_color",
                        name=f"{alias} {color_hue}".strip(),
                        brand=subject.brand,
                    )
            # Tier 2 hooks — executed only when Tier 1 matching misses.
            if subject.brand:
                requests.append(ProductRetrievalRequest(
                    strategy="brand_candidates",
                    brand=subject.brand,
                ))
            elif category_ids:
                for category_id in category_ids[:5]:
                    requests.append(ProductRetrievalRequest(
                        strategy="category_candidates",
                        category_id=str(category_id),
                    ))
        else:
            available = True
            available_in_store = True
            for index, category_id in enumerate(category_ids[:5]):
                requests.append(ProductRetrievalRequest(
                    strategy="category" if index == 0 else "category_child",
                    category_id=str(category_id),
                    brand=subject.brand,
                    available=available,
                    available_in_store=available_in_store,
                ))
            if subject.product_type:
                gender_tokens = preference_gender_tokens(interpretation)
                gender_label = gender_tokens[0] if gender_tokens else None
                # Prefer gendered catalog query ("relógio feminino") so Tray
                # surfaces the right segment before soft ranking.
                primary_name = (
                    f"{subject.product_type} {gender_label}".strip()
                    if gender_label
                    else subject.product_type
                )
                requests.append(ProductRetrievalRequest(
                    strategy="name_fallback",
                    name=primary_name,
                    brand=subject.brand,
                    available=available,
                    available_in_store=available_in_store,
                ))
                if gender_label and primary_name != subject.product_type:
                    requests.append(ProductRetrievalRequest(
                        strategy="name_fallback_category",
                        name=subject.product_type,
                        brand=subject.brand,
                        available=available,
                        available_in_store=available_in_store,
                    ))
            elif subject.brand:
                requests.append(ProductRetrievalRequest(
                    strategy="explicit_brand",
                    brand=subject.brand,
                    available=available,
                    available_in_store=available_in_store,
                ))

            # Color probes (PT/EN aliases) so Tray returns blue when user said azul.
            color_labels = preference_color_search_labels(interpretation)
            if subject.brand and color_labels:
                for label in color_labels[:4]:
                    requests.append(ProductRetrievalRequest(
                        strategy="color_brand_probe",
                        name=label,
                        brand=subject.brand,
                        available=available,
                        available_in_store=available_in_store,
                    ))
            elif color_labels and subject.product_type:
                for label in color_labels[:3]:
                    requests.append(ProductRetrievalRequest(
                        strategy="color_name_probe",
                        name=f"{subject.product_type} {label}".strip(),
                        brand=subject.brand,
                        available=available,
                        available_in_store=available_in_store,
                    ))

        discovery_pages = CATALOG_DISCOVERY_MAX_PAGES
        if preference_color_tokens(interpretation):
            # A few extra brand pages help surface color variants without
            # blowing the Vercel Hobby wall-clock budget.
            discovery_pages = max(discovery_pages, 8)

        return ProductRetrievalPlan(
            mode="exact" if exact else "recommendation",
            requests=tuple(requests),
            discovery_max_pages=discovery_pages,
            discovery_max_products=max(
                CATALOG_DISCOVERY_MAX_PRODUCTS,
                discovery_pages * PRODUCT_PAGE_LIMIT,
            ),
        )
