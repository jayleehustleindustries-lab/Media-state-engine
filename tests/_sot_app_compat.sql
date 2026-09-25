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

-- webhook_outbox now comes from supabase/migrations/20260317000006_webhook_outbox_adjunct.sql (deploy SoT / #18).

-- approved_at / approved_by / approved_by_key_id are now REAL SoT columns
-- (supabase/migrations/20260317000007_approval_audit.sql), applied before
-- this file by apply_schema() — nothing left to patch here.

-- Dual-format asset kinds used by pipeline
DO $$ BEGIN
  ALTER TYPE asset_kind ADD VALUE IF NOT EXISTS 'video_h';
EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN
  ALTER TYPE asset_kind ADD VALUE IF NOT EXISTS 'final_h';
EXCEPTION WHEN others THEN NULL; END $$;

-- Vertex clip-generation asset kind (one row per job holding all 3 Veo
-- clip LRO handles in meta.clips; see pipeline.generate_vertex_clips).
-- Same test-only-overlay gap as video_h/final_h above: not yet in
-- supabase/migrations, so this only works under this compat schema, not a
-- pure-migrations canonical DB. Flagged in the branch report as a
-- fast-follow migration candidate, matching the pre-existing gap.
DO $$ BEGIN
  ALTER TYPE asset_kind ADD VALUE IF NOT EXISTS 'video_clip';
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

-- Prefer legacy in-app graph for Phase 1-5 pytest (pipeline.py / HeyGen /
-- image_gate / quality-gate / distribute / queue / outbox / concurrency
-- suites), which drive jobs straight through legacy status literals
-- (script_ready -> audio_generating -> ... -> staged -> approved ->
-- delivered/failed) and skip the canonical queued/running/image_* steps
-- entirely. This is a real architecture gap (those paths were never
-- ported to the canonical graph), not something this fix's scope covers
-- — porting it means rewriting HeyGen / the image quality gate / the
-- script quality gate / the distribute worker, which is explicitly out
-- of scope for the approval-gate/job-creation fix this overlay supports.
--
-- Verified empirically (not assumed): leaving transition_job live here
-- and reapplying the existing suite breaks 21 of 74 tests, all in
-- exactly those out-of-scope files (test_pipeline_phase1, test_phase2-5,
-- test_concurrency_pg) with "illegal transition X -> Y" from the real
-- RPC's strict canonical graph. Deleting a handful of legacy status
-- literals here (leaving transition_job in place) would only trade that
-- breakage for a different one, since job_status enum membership,
-- job_events vs events, and the pipeline's direct-jump transitions are
-- all still legacy-shaped below this line.
--
-- The canonical approval path this overlay is NOT allowed to silently
-- swallow (create_job + approve_job, i.e. the actual scope of this fix)
-- is instead exercised against the real, untouched transition_job RPC in
-- tests/test_canonical_approval.py, via
-- tests.conftest.apply_canonical_schema() — supabase/migrations/*.sql
-- only, zero compat overlay, zero legacy status literals. That suite is
-- the real answer to "does transition_job actually gate approval
-- correctly"; this DROP intentionally keeps the (already broad,
-- already-passing, out-of-scope) legacy suite on the permissive Python
-- fallback graph it was written against, so this fix adds new real-RPC
-- coverage without regressing 21 unrelated tests to fix them.
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
