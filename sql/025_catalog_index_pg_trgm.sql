-- Fuzzy lexical search on the durable catalog index.
-- Safe to skip if the role cannot CREATE EXTENSION; retrieval falls back to LIKE.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_title_trgm
ON public.ai_catalog_index
USING gin ((lower(title_normalized)) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_model_trgm
ON public.ai_catalog_index
USING gin ((lower(coalesce(model, ''))) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_reference_trgm
ON public.ai_catalog_index
USING gin ((lower(coalesce(reference, ''))) gin_trgm_ops);
