CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  script_text text NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','script_ready','audio_generating','audio_ready','rendering','rendered','delivered','failed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('audio','video','final')),
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
    CHECK (step IN ('generate_audio','generate_avatar','render','reconcile','flush_outbox')),
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
