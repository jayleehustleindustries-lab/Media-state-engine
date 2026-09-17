CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  script_text text NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','script_ready','audio_generating','audio_ready','rendering','rendered','delivered','failed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('audio','video','final')), url text, storage_path text,
  meta jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (job_id, kind)
);
CREATE TABLE IF NOT EXISTS events (
  id bigserial PRIMARY KEY, job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  from_status text, to_status text NOT NULL, payload jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key text PRIMARY KEY, job_id uuid NOT NULL REFERENCES jobs(id), step text NOT NULL,
  result jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_assets_job ON assets(job_id);
