-- Fuzzy lexical search on the durable catalog index.
-- Safe to skip if the role cannot CREATE EXTENSION; retrieval falls back to LIKE.

CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_title_trgm
ON public.ai_catalog_index
USING gin ((lower(coalesce(title_normalized, ''))) extensions.gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_model_trgm
ON public.ai_catalog_index
USING gin ((lower(coalesce(model, ''))) extensions.gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_reference_trgm
ON public.ai_catalog_index
USING gin ((lower(coalesce(reference, ''))) extensions.gin_trgm_ops);
