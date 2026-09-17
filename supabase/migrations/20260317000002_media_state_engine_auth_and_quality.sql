-- Media State Engine — migration 002: auth, quality scores, DLQ/retry policy, core patches
-- Target: Supabase (Postgres 15+)
-- Depends on: 001_media_state_engine_core.sql
-- Run after 001. Idempotent where noted.
-- Does NOT store provider secrets (HeyGen/ElevenLabs) — only app API keys (hashed).

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- (a) API keys for FastAPI bearer auth
-- Store ONLY a hash. App compares sha256(presented_key) to key_hash.
-- ---------------------------------------------------------------------------
create table if not exists api_keys (
  id              uuid primary key default gen_random_uuid(),
  name            text not null,                    -- human label, e.g. 'fastapi-prod'
  key_prefix      text not null,                    -- first 8 chars for lookup/logs (not secret)
  key_hash        text not null,                    -- encode(digest(raw_key, 'sha256'), 'hex')
  scopes          text[] not null default '{jobs:read,jobs:write,admin}',
  is_active       boolean not null default true,
  expires_at      timestamptz,
  last_used_at    timestamptz,
  created_by      text,
  meta            jsonb not null default '{}'::jsonb,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  constraint api_keys_prefix_uidx unique (key_prefix),
  constraint api_keys_hash_uidx unique (key_hash)
);

create index if not exists api_keys_active_idx on api_keys (is_active) where is_active = true;

drop trigger if exists trg_api_keys_updated on api_keys;
create trigger trg_api_keys_updated before update on api_keys
  for each row execute function set_updated_at();

-- Helper: verify a presented raw key. Returns key row or raises.
create or replace function verify_api_key(p_raw_key text)
returns api_keys
language plpgsql
security definer
set search_path = public
as $$
declare
  v_hash text;
  v_row  api_keys;
begin
  if p_raw_key is null or length(trim(p_raw_key)) < 16 then
    raise exception 'invalid api key' using errcode = '28000';
  end if;
  v_hash := encode(digest(trim(both from p_raw_key), 'sha256'), 'hex');
  select * into v_row from api_keys
   where key_hash = v_hash and is_active = true
     and (expires_at is null or expires_at > now())
   for update;
  if not found then
    raise exception 'invalid api key' using errcode = '28000';
  end if;
  update api_keys set last_used_at = now() where id = v_row.id;
  return v_row;
end;
$$;

revoke all on function verify_api_key(text) from public;
do $$ begin
  if exists (select 1 from pg_roles where rolname='authenticated') then
    grant execute on function verify_api_key(text) to authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname='service_role') then
    grant execute on function verify_api_key(text) to service_role;
  end if;
  grant execute on function verify_api_key(text) to public;
end $$;

-- ---------------------------------------------------------------------------
-- (b) Content quality scores — score scripts BEFORE render (save paid calls)
-- ---------------------------------------------------------------------------
do $$ begin
  create type quality_verdict as enum ('pass','revise','reject');
exception when duplicate_object then null; end $$;

create table if not exists content_quality_scores (
  id              uuid primary key default gen_random_uuid(),
  job_id          uuid not null references jobs(id) on delete cascade,
  script_hash     text not null,                    -- must match jobs.script_hash at score time
  -- 0–100 integers; app enforces semantics
  hook_strength   smallint not null check (hook_strength between 0 and 100),
  clarity         smallint not null check (clarity between 0 and 100),
  cta             smallint not null check (cta between 0 and 100),
  pacing          smallint not null check (pacing between 0 and 100),
  overall         smallint generated always as (
                    ((hook_strength + clarity + cta + pacing) / 4)
                  ) stored,
  verdict         quality_verdict not null,
  scorer          text not null default 'bot',       -- 'bot','human','model:<name>'
  notes           text,
  rubric_version  text not null default 'v1',
  meta            jsonb not null default '{}'::jsonb,
  created_at      timestamptz not null default now()
);

create index if not exists cqs_job_idx on content_quality_scores (job_id, created_at desc);
create index if not exists cqs_verdict_idx on content_quality_scores (verdict);

-- Only one "latest" score isn't enforced uniquely; workers should order by created_at desc.
-- Optional: block enqueue to tts/render unless latest verdict = pass (enforced in app or gate RPC below).

create or replace function latest_quality_verdict(p_job_id uuid)
returns quality_verdict
language sql stable as $$
  select verdict from content_quality_scores
   where job_id = p_job_id
   order by created_at desc
   limit 1
$$;

-- ---------------------------------------------------------------------------
-- (c) Dead-letter / retry policy (declarative) + job_dead_letters (instances)
-- ---------------------------------------------------------------------------
create table if not exists retry_policies (
  id              text primary key,                 -- e.g. 'tts_default','render_heygen','webhook_outbox'
  max_attempts    integer not null default 5 check (max_attempts >= 1),
  base_delay_sec  integer not null default 30 check (base_delay_sec >= 1),
  max_delay_sec   integer not null default 3600,
  jitter_pct      smallint not null default 20 check (jitter_pct between 0 and 100),
  dead_after      integer not null default 5,       -- move to DLQ when attempts >= this
  meta            jsonb not null default '{}'::jsonb,
  updated_at      timestamptz not null default now()
);

insert into retry_policies (id, max_attempts, base_delay_sec, max_delay_sec, dead_after) values
  ('tts_default', 5, 30, 1800, 5),
  ('render_heygen', 3, 60, 3600, 3),
  ('render_remotion', 5, 30, 1800, 5),
  ('webhook_outbox', 8, 15, 7200, 8)
on conflict (id) do nothing;

create table if not exists job_dead_letters (
  id              bigserial primary key,
  job_id          uuid not null references jobs(id) on delete cascade,
  from_status     job_status not null,
  policy_id       text references retry_policies(id) on delete set null,
  attempt         integer not null,
  last_error      text,
  payload         jsonb not null default '{}'::jsonb,
  requeued_at     timestamptz,                      -- null until operator requeues
  created_at      timestamptz not null default now()
);

create index if not exists jdl_open_idx on job_dead_letters (job_id)
  where requeued_at is null;

-- ---------------------------------------------------------------------------
-- (d) Missing constraints / indexes / patches on core schema
-- ---------------------------------------------------------------------------

-- Webhook delivery status as constrained text (enum-like)
do $$ begin
  alter table webhook_deliveries
    add constraint wh_status_chk
    check (status in ('pending','success','failed','dead'));
exception when duplicate_object then null; end $$;

-- Dedup outbound webhooks for same job+target+endpoint while pending
create unique index if not exists wh_pending_dedupe_uidx
  on webhook_deliveries (job_id, target, endpoint)
  where status in ('pending','failed');

-- Sweeper / queue claim helpers
create index if not exists jobs_running_lock_idx
  on jobs (status, locked_at)
  where status in ('tts_running','render_running');

create index if not exists jobs_queue_claim_idx
  on jobs (status, scheduled_for nulls first, created_at)
  where status in ('tts_queued','render_queued');

-- Provider external id lookup (HeyGen video_id reconcile)
create index if not exists assets_external_id_idx
  on assets (provider, external_id)
  where external_id is not null;

-- Require url or storage_path once bytes claimed (soft: only when bytes > 0)
do $$ begin
  alter table assets
    add constraint assets_locator_chk
    check (
      bytes is null or bytes = 0
      or url is not null
      or storage_path is not null
    );
exception when duplicate_object then null; end $$;

-- Job ↔ asset kind sanity (optional soft FKs already exist; add aspect helper columns)
alter table jobs
  add column if not exists aspect_primary text not null default '9:16',
  add column if not exists include_horizontal boolean not null default false,
  add column if not exists cost_estimate_usd numeric(10,4),
  add column if not exists provider_latency_ms integer;

-- Extend asset_kind with final (delivered artifact) if missing — Postgres enums need ADD VALUE
do $$ begin
  alter type asset_kind add value if not exists 'final';
exception when others then null; end $$;

-- ---------------------------------------------------------------------------
-- Harder transition_job: close auto-publish holes, clear locks, honor max_attempts,
-- optional quality gate before leaving draft / entering tts_queued
-- ---------------------------------------------------------------------------
create or replace function transition_job(
  p_job_id        uuid,
  p_to            job_status,
  p_actor         text default 'system',
  p_reason        text default null,
  p_payload       jsonb default '{}'::jsonb,
  p_error         text default null,
  p_set_locked_by text default null,
  p_require_quality_pass boolean default true
) returns jobs
language plpgsql as $$
declare
  v_from job_status;
  v_row  jobs;
  v_verdict quality_verdict;
begin
  select status into v_from from jobs where id = p_job_id for update;
  if not found then
    raise exception 'job % not found', p_job_id using errcode = 'P0002';
  end if;

  -- LEGAL GRAPH (002): never skip review/approved for public publish
  if not (
    (v_from = 'draft'          and p_to in ('tts_queued','render_queued','cancelled')) or
    (v_from = 'tts_queued'     and p_to in ('tts_running','tts_failed','cancelled')) or
    (v_from = 'tts_running'    and p_to in ('tts_done','tts_failed','cancelled')) or
    (v_from = 'tts_done'       and p_to in ('render_queued','cancelled')) or
    (v_from = 'tts_failed'     and p_to in ('tts_queued','dead','cancelled')) or
    (v_from = 'render_queued'  and p_to in ('render_running','render_failed','cancelled')) or
    (v_from = 'render_running' and p_to in ('render_done','render_failed','cancelled')) or
    (v_from = 'render_done'    and p_to in ('review','cancelled')) or
    (v_from = 'render_failed'  and p_to in ('render_queued','dead','cancelled')) or
    (v_from = 'review'         and p_to in ('approved','cancelled','render_queued')) or
    (v_from = 'approved'       and p_to in ('published','cancelled')) or
    (v_from = 'published'      and p_to in ('cancelled')) or
    (v_from = 'cancelled'      and p_to in ('draft')) or
    (v_from = 'dead'           and p_to in ('tts_queued','render_queued','cancelled'))
  ) then
    raise exception 'illegal transition % -> %', v_from, p_to using errcode = 'P0001';
  end if;

  -- Quality gate: entering paid pipeline from draft requires latest verdict = pass
  if p_require_quality_pass and v_from = 'draft' and p_to in ('tts_queued','render_queued') then
    v_verdict := latest_quality_verdict(p_job_id);
    if v_verdict is distinct from 'pass' then
      raise exception 'quality gate blocked: latest verdict=% (need pass)', coalesce(v_verdict::text, 'null')
        using errcode = 'P0003';
    end if;
  end if;

  update jobs
     set status      = p_to,
         last_error  = case when p_error is not null then p_error
                            when p_to in ('tts_done','render_done','approved','published') then null
                            else last_error end,
         error_history = case when p_error is not null
                              then error_history || jsonb_build_array(
                                jsonb_build_object('at', now(), 'from', v_from, 'to', p_to, 'error', p_error))
                              else error_history end,
         locked_by   = case
                         when p_to in ('tts_running','render_running') then coalesce(p_set_locked_by, locked_by)
                         when p_to in ('tts_done','tts_failed','render_done','render_failed','cancelled','dead') then null
                         else locked_by
                       end,
         locked_at   = case
                         when p_to in ('tts_running','render_running') then now()
                         when p_to in ('tts_done','tts_failed','render_done','render_failed','cancelled','dead') then null
                         else locked_at
                       end,
         attempt     = case when p_to in ('tts_failed','render_failed') then attempt + 1 else attempt end,
         published_at = case when p_to = 'published' then coalesce(published_at, now()) else published_at end
   where id = p_job_id
   returning * into v_row;

  -- Auto-dead when attempts exhausted on failure transitions
  if p_to in ('tts_failed','render_failed') and v_row.attempt >= v_row.max_attempts then
    update jobs set status = 'dead' where id = p_job_id returning * into v_row;
    insert into job_events (job_id, from_status, to_status, actor, reason, payload)
    values (p_job_id, p_to, 'dead', coalesce(p_actor,'system'), 'max_attempts exceeded', p_payload);
    insert into job_dead_letters (job_id, from_status, policy_id, attempt, last_error, payload)
    values (
      p_job_id, p_to,
      case when p_to = 'tts_failed' then 'tts_default' else 'render_heygen' end,
      v_row.attempt, v_row.last_error, p_payload
    );
    return v_row;
  end if;

  insert into job_events (job_id, from_status, to_status, actor, reason, payload)
  values (p_job_id, v_from, p_to, p_actor, p_reason, p_payload);

  return v_row;
end;
$$;

-- Atomic claim: queued → running with SKIP LOCKED (prevents double paid provider calls)
create or replace function claim_job(
  p_from job_status,
  p_to   job_status,
  p_worker text,
  p_limit integer default 1
) returns setof jobs
language plpgsql as $$
begin
  if not (
    (p_from = 'tts_queued' and p_to = 'tts_running') or
    (p_from = 'render_queued' and p_to = 'render_running')
  ) then
    raise exception 'claim_job only supports queued→running' using errcode = 'P0001';
  end if;

  return query
  with picked as (
    select id from jobs
     where status = p_from
       and (scheduled_for is null or scheduled_for <= now())
     order by scheduled_for nulls first, created_at
     for update skip locked
     limit greatest(p_limit, 1)
  )
  update jobs j
     set status = p_to,
         locked_by = p_worker,
         locked_at = now()
    from picked p
   where j.id = p.id
  returning j.*;

  -- events for claimed rows
  insert into job_events (job_id, from_status, to_status, actor, reason)
  select id, p_from, p_to, p_worker, 'claim'
    from jobs
   where locked_by = p_worker and status = p_to and locked_at > now() - interval '2 seconds';
end;
$$;

-- Fixed reclaim: route through transition_job semantics + write clean events
create or replace function reclaim_stuck_jobs(p_stale_minutes integer default 15)
returns integer
language plpgsql as $$
declare
  r record;
  n integer := 0;
  v_to job_status;
begin
  for r in
    select id, status from jobs
     where status in ('tts_running','render_running')
       and locked_at < now() - make_interval(mins => p_stale_minutes)
     for update skip locked
  loop
    v_to := case when r.status = 'tts_running' then 'tts_failed'::job_status
                 else 'render_failed'::job_status end;
    perform transition_job(
      r.id, v_to, 'system', 'stale lock reclaim',
      '{}'::jsonb, 'reclaimed: worker lock stale', null, false
    );
    n := n + 1;
  end loop;
  return n;
end;
$$;

-- ---------------------------------------------------------------------------
-- Minimal RLS posture (Supabase): deny anon; service_role bypasses RLS
-- Enable RLS but no policies for anon/authenticated → only service_role works.
-- Tighten later with per-key policies if needed.
-- ---------------------------------------------------------------------------
alter table assets enable row level security;
alter table jobs enable row level security;
alter table job_events enable row level security;
alter table webhook_deliveries enable row level security;
alter table api_keys enable row level security;
alter table content_quality_scores enable row level security;
alter table retry_policies enable row level security;
alter table job_dead_letters enable row level security;

-- service_role bypasses RLS by default in Supabase; no open policies on purpose.
