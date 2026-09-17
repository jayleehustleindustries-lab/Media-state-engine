-- Test-only overlay after supabase SoT packs.
-- Phase 1–5 app code still uses legacy table names, columns, and status strings.
-- Production deploy does NOT apply this file (see scripts/apply_migrations.sh).

-- Legacy statuses on SoT enum so dual-mode inserts/advances work
DO $$ BEGIN
  ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'pending';
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN
  ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'script_ready';
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN
  ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'audio_generating';
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN
  ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'audio_ready';
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN
  ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'rendering';
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN
  ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'rendered';
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN
  ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'staged';
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN
  ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'delivered';
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN
  ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'failed';
EXCEPTION WHEN others THEN NULL; END $$;

-- Jobs: app INSERT omits SoT-required keys; needs script jsonb
ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS script jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE jobs
  ALTER COLUMN idempotency_key SET DEFAULT encode(gen_random_bytes(16), 'hex');
ALTER TABLE jobs
  ALTER COLUMN script_hash SET DEFAULT '';

-- Assets: legacy rows are job-scoped
ALTER TABLE assets
  ADD COLUMN IF NOT EXISTS job_id uuid REFERENCES jobs(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS assets_job_id_idx ON assets (job_id);

-- Legacy event log name (get_job_detail / tests SELECT events)
CREATE TABLE IF NOT EXISTS events (
  id bigserial PRIMARY KEY,
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  from_status text,
  to_status text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events (job_id, created_at);

-- App outbox (SoT has webhook_deliveries with different columns)
CREATE TABLE IF NOT EXISTS webhook_outbox (
  id bigserial PRIMARY KEY,
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  destination_url text NOT NULL,
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','delivering','delivered','dead')),
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 8,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON webhook_outbox (status, next_attempt_at)
  WHERE status IN ('pending', 'delivering');


-- Approve metadata used by app.services.jobs.approve_job
ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS approved_at timestamptz,
  ADD COLUMN IF NOT EXISTS approved_by text;

-- Dual-format asset kinds used by pipeline
DO $$ BEGIN
  ALTER TYPE asset_kind ADD VALUE IF NOT EXISTS 'video_h';
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN
  ALTER TYPE asset_kind ADD VALUE IF NOT EXISTS 'final_h';
EXCEPTION WHEN others THEN NULL; END $$;

-- Unique needed by pipeline ON CONFLICT (job_id, kind)
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'assets_job_id_kind_key'
  ) THEN
    ALTER TABLE assets ADD CONSTRAINT assets_job_id_kind_key UNIQUE (job_id, kind);
  END IF;
EXCEPTION WHEN others THEN
  -- job_id may be null on pre-existing SoT-only rows; partial unique instead
  CREATE UNIQUE INDEX IF NOT EXISTS assets_job_id_kind_uidx
    ON assets (job_id, kind) WHERE job_id IS NOT NULL;
END $$;

-- Prefer legacy in-app graph for Phase 1–5 pytest until create_job/outbox ported to SoT RPC
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT p.oid::regprocedure AS sig
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE p.proname = 'transition_job' AND n.nspname = 'public'
  LOOP
    EXECUTE 'DROP FUNCTION IF EXISTS ' || r.sig;
  END LOOP;
END $$;
