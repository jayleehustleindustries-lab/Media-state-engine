-- Phase 4: daily cadence scheduler + metrics (+ distribute stays on work_queue)

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
