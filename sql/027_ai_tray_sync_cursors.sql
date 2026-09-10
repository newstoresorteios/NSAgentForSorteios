-- Durable cursor for incremental Tray webhook ingestion.
-- Production keeps AUTO_CREATE_TABLES disabled, so this table must be migrated.
CREATE TABLE IF NOT EXISTS public.ai_tray_sync_cursors (
    cursor_key text PRIMARY KEY,
    last_event_id bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

