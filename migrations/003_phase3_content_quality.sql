-- Phase 3: structured script, formats, platform captions, approval statuses

-- Expand job status machine: staged (awaiting human approval) + approved
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (
  status IN (
    'pending','script_ready','audio_generating','audio_ready',
    'rendering','rendered','staged','approved','delivered','failed'
  )
);

-- Structured script fields (hook / body / cta / duration / aspect)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS script jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Dual-format + platform caption staging (never secrets)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS meta jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Approval audit
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS approved_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS approved_by text;

-- Asset kinds: horizontal sibling variants (opt-in dual HeyGen)
ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_kind_check;
ALTER TABLE assets ADD CONSTRAINT assets_kind_check CHECK (
  kind IN ('audio','video','final','video_h','final_h')
);

-- work_queue: distribute stub (Phase 3 stub / Phase 4 real publish)
ALTER TABLE work_queue DROP CONSTRAINT IF EXISTS work_queue_step_check;
ALTER TABLE work_queue ADD CONSTRAINT work_queue_step_check CHECK (
  step IN ('generate_audio','generate_avatar','render','reconcile','flush_outbox','distribute')
);
