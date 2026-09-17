-- Image gate caps: daily scorer budget + break-glass durable audit (reaudit FAIL closures)

CREATE TABLE IF NOT EXISTS image_scorer_budget (
  day_utc date PRIMARY KEY,
  used integer NOT NULL DEFAULT 0 CHECK (used >= 0),
  budget_limit integer NOT NULL CHECK (budget_limit >= 0),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS image_gate_break_glass_audit (
  id bigserial PRIMARY KEY,
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  actor text NOT NULL DEFAULT 'system',
  reason text,
  env_flag boolean NOT NULL DEFAULT true,
  job_flag boolean NOT NULL DEFAULT true,
  meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS image_gate_break_glass_audit_job_idx
  ON image_gate_break_glass_audit (job_id, created_at DESC);

COMMENT ON TABLE image_scorer_budget IS
  'UTC-day durable counter for vision scorer calls; refuse when used >= budget_limit';
COMMENT ON TABLE image_gate_break_glass_audit IS
  'Durable audit for IMAGE_GATE_BREAK_GLASS dual-control bypass (env + per-job flag)';
