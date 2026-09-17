-- App adjuncts on top of Supabase packs 001+002.
-- Keeps schedule/metrics/idempotency/work helpers without forking the SoT status graph.

create table if not exists idempotency_keys (
  key text primary key,
  job_id uuid references jobs(id) on delete cascade,
  step text not null,
  result jsonb,
  created_at timestamptz not null default now(),
  expires_at timestamptz
);
create index if not exists idempotency_expires_idx
  on idempotency_keys (expires_at) where result is null;

-- Lightweight work queue for non-claim_job steps (image gate, distribute, outbox flush)
create table if not exists work_queue (
  id bigserial primary key,
  job_id uuid not null references jobs(id) on delete cascade,
  step text not null check (step in (
    'score_image','revise_image','generate_audio','generate_avatar',
    'render','reconcile','flush_outbox','distribute'
  )),
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'pending'
    check (status in ('pending','running','done','failed','dead')),
  attempts int not null default 0,
  max_attempts int not null default 5,
  last_error text,
  next_attempt_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);
create unique index if not exists work_queue_active_job_step_uidx
  on work_queue (job_id, step) where status in ('pending','running');
create index if not exists work_queue_pending_idx
  on work_queue (status, next_attempt_at) where status in ('pending','running');

drop trigger if exists trg_work_queue_updated on work_queue;
create trigger trg_work_queue_updated before update on work_queue
  for each row execute function set_updated_at();

-- Daily schedule cadence (Phase 4)
create table if not exists schedule_runs (
  id bigserial primary key,
  run_date date not null,
  slot int not null,
  job_id uuid references jobs(id) on delete set null,
  topic text,
  meta jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (run_date, slot)
);

create table if not exists metric_counters (
  name text primary key,
  value bigint not null default 0,
  labels jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create table if not exists metric_samples (
  id bigserial primary key,
  name text not null,
  value double precision not null,
  labels jsonb not null default '{}'::jsonb,
  recorded_at timestamptz not null default now()
);
create index if not exists metric_samples_name_time_idx
  on metric_samples (name, recorded_at desc);

-- Provider cost log (ElevenLabs / Remotion / HeyGen)
create table if not exists provider_call_log (
  id bigserial primary key,
  job_id uuid references jobs(id) on delete set null,
  provider text not null,
  operation text not null,
  idempotency_key text,
  status text not null default 'ok',
  latency_ms integer,
  cost_usd numeric(12,6),
  meta jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists provider_call_log_job_idx on provider_call_log (job_id, created_at desc);
