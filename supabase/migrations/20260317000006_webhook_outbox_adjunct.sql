-- App adjunct: durable webhook outbox used by app/services/outbox.py
-- (claim / backoff / DLQ / F4 reclaim). SoT pack 001 also has webhook_deliveries
-- for canonical fan-out; the Python outbox path still targets webhook_outbox.
-- See audit #18.
--
-- No BEFORE UPDATE set_updated_at trigger: F4 reclaim and tests set updated_at
-- explicitly for staleness; a trigger would overwrite those values (unlike
-- work_queue, which keys reclaim off started_at).

create table if not exists webhook_outbox (
  id bigserial primary key,
  job_id uuid not null references jobs(id) on delete cascade,
  destination_url text not null,
  payload jsonb not null,
  status text not null default 'pending'
    check (status in ('pending','delivering','delivered','dead')),
  attempts int not null default 0,
  max_attempts int not null default 8,
  next_attempt_at timestamptz not null default now(),
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  delivered_at timestamptz
);

create index if not exists idx_outbox_pending on webhook_outbox (status, next_attempt_at)
  where status in ('pending', 'delivering');
