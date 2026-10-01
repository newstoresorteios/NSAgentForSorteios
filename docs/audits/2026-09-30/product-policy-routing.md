# Product policy / informational payment regression — 2026-09-30

## Scope

Local implementation for the Instagram sequence: named PRX Powermatic 80,
35 mm, blue; manufacturer-warranty/documentation question; first-purchase
discount; repetition of both questions. No customer messages, database writes,
deploys, paid model calls or pushes were performed for this implementation.

## Changes

- Informational discount/payment policy runs before product-selection repair.
  Explicit cart mutations, checkout, order/shipping queries and installment
  simulations remain on their existing routes. The published runtime supplies
  the percentage (including zero); no extra first-purchase coupon is invented.
- A policy reply leaves the previous cart, shortlist and preferences unchanged.
- `primeira compra` is not a reference to shortlist option 1. Selection prompts
  list only actual presented products; an empty shortlist never yields 1/2/3.
- Product browsing, an active SKU or a media request is not proof of live
  checkout. Actual cart targets, pending orders and checkout actions still are.
- Mixed product/warranty questions keep the catalog lookup and original product
  constraints. The answer combines approved catalog evidence and the current
  published warranty coverage. Missing manufacturer activation or a complete
  document list is stated explicitly; no blanket manufacturer warranty promise.
- The composed answer is protected by the offer contract. Rejected SKUs/prices
  cannot return through a policy rewrite. Existing inspection specifications
  remain in the response. Product warranty/document fields survive factual
  projection only through the same commercial-authority checks as other facts.
- The factual validator recognizes the exact deterministic payment-policy
  answer rebuilt from the current runtime. Forged percentages, coupons, changed
  policy metadata or a flag alone do not satisfy this authority.

## Verification

`tests/ops/test_product_policy_incident.py` exercises the four-turn sequence
through sales routing, answer council, factual validation, deterministic double
check, handoff consent, outbound composition and final validation. Catalog and
AI boundaries are stubbed; this is not a live-production replay or a new stock
confirmation. Additional tests cover mutated offers, unknown and confirmed
product fields, current policy changes, zero discount, actual list choices,
human requests and preservation of existing checkout/mutation routes.

Broad regression coverage: sales, commerce, verify, catalog, persona, agents,
stories, ingress and ops. External socket connections are blocked for tests.
Final run: **1,959 passed, 1 explicitly deselected, 8 warnings**. The new incident
module contains 32 regression cases. `git diff --check` passed.

Pre-existing failure confirmed at baseline
`e1279685319f17dd4e3403f7893ae37411a6487b`:
`tests/catalog/test_compiled_ranking_authority.py::test_catalog_does_not_import_sales`.
`app/catalog/vision/image_product_id.py` already imports
`app.sales.after_sales_support`. This architecture-boundary failure is unrelated
to these changes; the final broad run excludes that single test explicitly.

Concurrent changes to `sql/seeds/operator_catalog.json`, the story-configuration
migration and its configuration test were not made by this implementation and
were left intact. No operator-catalog migration is required by this fix.
