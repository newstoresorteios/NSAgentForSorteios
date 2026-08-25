-- Add structured water resistance for diver assertiveness (IQ-06 / 25/08 Certina).
ALTER TABLE public.ai_catalog_index
  ADD COLUMN IF NOT EXISTS water_resistance_m integer NULL;

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_wr
  ON public.ai_catalog_index (tenant_id, water_resistance_m)
  WHERE water_resistance_m IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_case_size
  ON public.ai_catalog_index (tenant_id, case_size)
  WHERE case_size IS NOT NULL;

-- Best-effort backfill from normalized titles (many rows lack mm/WR in title).
UPDATE public.ai_catalog_index
SET case_size = (regexp_match(title_normalized, '([2-5][0-9])\s*mm'))[1]
WHERE case_size IS NULL
  AND title_normalized ~ '[2-5][0-9]\s*mm';

UPDATE public.ai_catalog_index
SET water_resistance_m = ((regexp_match(title_normalized, '(200|300|500|1000)\s*m'))[1])::integer
WHERE water_resistance_m IS NULL
  AND title_normalized ~ '(200|300|500|1000)\s*m';

UPDATE public.ai_catalog_index
SET water_resistance_m = 100
WHERE water_resistance_m IS NULL
  AND title_normalized ~ '\y100\s*m\y'
  AND title_normalized !~ '(200|300)\s*m';

-- Line heuristics when title omits meters (Certina DS Action / DS-7, Baltic Aquascaphe).
UPDATE public.ai_catalog_index
SET water_resistance_m = 200
WHERE water_resistance_m IS NULL
  AND (
    title_normalized ~ 'ds action'
    OR title_normalized ~ 'aquascaphe'
    OR title_normalized ~ 'seastar'
    OR title_normalized ~ 'prospex'
  );

UPDATE public.ai_catalog_index
SET water_resistance_m = 100
WHERE water_resistance_m IS NULL
  AND (
    title_normalized ~ 'ds-7'
    OR title_normalized ~ 'ds 7'
  );
