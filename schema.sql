-- =============================================================================
-- LEGACY schema dump (NOT the deploy source of truth)
-- New environments: apply supabase/migrations/*.sql via ./scripts/apply_migrations.sh
-- (see docs/DEPLOY.md). Do not treat this file + migrations/00x_phase*.sql as SoT.
-- Outside-repo Drive packs under supabase-schema/ are also not the install path.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  script_text text NOT NULL,
  script jsonb NOT NULL DEFAULT '{}'::jsonb,
  meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN (
    'pending','script_ready','audio_generating','audio_ready',
    'rendering','rendered','staged','approved','delivered','failed'
  )),
  approved_at timestamptz,
  approved_by text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('audio','video','final','video_h','final_h')),
  url text,
  storage_path text,
  meta jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, kind)
);

CREATE TABLE IF NOT EXISTS events (
  id bigserial PRIMARY KEY,
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  from_status text,
  to_status text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  key text PRIMARY KEY,
  job_id uuid NOT NULL REFERENCES jobs(id),
  step text NOT NULL,
  result jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz
);

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

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_status_updated ON jobs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_assets_job ON assets(job_id);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON webhook_outbox(status, next_attempt_at)
  WHERE status IN ('pending', 'delivering');
CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency_keys(expires_at)
  WHERE result IS NULL;

CREATE TABLE IF NOT EXISTS work_queue (
  id bigserial PRIMARY KEY,
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  step text NOT NULL
    CHECK (step IN ('generate_audio','generate_avatar','render','reconcile','flush_outbox','distribute')),
  payload jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','done','failed','dead')),
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 5,
  last_error text,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_work_queue_active_job_step
  ON work_queue(job_id, step)
  WHERE status IN ('pending', 'running');

CREATE INDEX IF NOT EXISTS idx_work_queue_pending
  ON work_queue(status, next_attempt_at)
  WHERE status IN ('pending', 'running');

CREATE INDEX IF NOT EXISTS idx_work_queue_job
  ON work_queue(job_id, created_at);

-- Phase 4: scheduler cadence + metrics
CREATE TABLE IF NOT EXISTS schedule_runs (
  id bigserial PRIMARY KEY,
  run_date date NOT NULL,
  slot int NOT NULL,
  job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
  topic text,
  meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_date, slot)
);

CREATE INDEX IF NOT EXISTS idx_schedule_runs_date ON schedule_runs(run_date);

CREATE TABLE IF NOT EXISTS metric_counters (
  name text PRIMARY KEY,
  value bigint NOT NULL DEFAULT 0,
  labels jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS metric_samples (
  id bigserial PRIMARY KEY,
  name text NOT NULL,
  value double precision NOT NULL,
  labels jsonb NOT NULL DEFAULT '{}'::jsonb,
  recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_metric_samples_name_time
  ON metric_samples(name, recorded_at DESC);
