-- Phase 2: Postgres-backed background work queue
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

-- At most one active (pending/running) work item per job+step
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_queue_active_job_step
  ON work_queue(job_id, step)
  WHERE status IN ('pending', 'running');

CREATE INDEX IF NOT EXISTS idx_work_queue_pending
  ON work_queue(status, next_attempt_at)
  WHERE status IN ('pending', 'running');

CREATE INDEX IF NOT EXISTS idx_work_queue_job
  ON work_queue(job_id, created_at);
