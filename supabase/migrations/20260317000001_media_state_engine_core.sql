-- Media State Engine — core schema
-- Target: Supabase (Postgres 15+)
-- Run in order. Idempotent where noted.

create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------
do $$ begin
  create type job_status as enum (
    'draft',
    'tts_queued','tts_running','tts_done','tts_failed',
    'render_queued','render_running','render_done','render_failed',
    'review','approved','published','cancelled','dead'
  );
exception when duplicate_object then null; end $$;

do $$ begin
  create type renderer_type as enum ('remotion','heygen');
exception when duplicate_object then null; end $$;

do $$ begin
  create type asset_kind as enum ('script','audio','video','thumbnail','caption','other');
exception when duplicate_object then null; end $$;

do $$ begin
  create type asset_provider as enum ('elevenlabs','heygen','remotion','upload','other');
exception when duplicate_object then null; end $$;

-- ---------------------------------------------------------------------------
-- assets  — canonical registry (no bytes in DB; store URLs + hashes)
-- ---------------------------------------------------------------------------
create table if not exists assets (
  id            uuid primary key default gen_random_uuid(),
  kind          asset_kind not null,
  provider      asset_provider not null default 'other',
  external_id   text,                       -- provider-side id (e.g. ElevenLabs voice/request)
  url           text,                       -- signed or public URL
  storage_path  text,                       -- e.g. s3://bucket/key
  content_hash  text,                       -- sha256 of bytes for dedup
  mime_type     text,
  bytes         bigint,
  duration_ms   integer,
  meta          jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create unique index if not exists assets_hash_uidx on assets (content_hash) where content_hash is not null;
create index if not exists assets_kind_idx on assets (kind);
create index if not exists assets_provider_idx on assets (provider);

-- ---------------------------------------------------------------------------
-- jobs  — one row per renderable unit of work
-- ---------------------------------------------------------------------------
create table if not exists jobs (
  id              uuid primary key default gen_random_uuid(),
  status          job_status not null default 'draft',
  renderer        renderer_type not null default 'remotion',

  -- idempotency: same inputs => same job
  idempotency_key text not null,
  script_hash     text not null,            -- hash of normalized script text
  voice_id        text,                     -- ElevenLabs / HeyGen voice
  template_id     text,                     -- Remotion composition or HeyGen template
  avatar_id       text,                     -- HeyGen avatar (nullable for Remotion)

  -- content
  script_text     text not null,
  script_asset_id uuid references assets(id) on delete set null,
  audio_asset_id  uuid references assets(id) on delete set null,
  video_asset_id  uuid references assets(id) on delete set null,
  thumb_asset_id  uuid references assets(id) on delete set null,

  -- scheduling / distribution
  scheduled_for   timestamptz,
  published_at    timestamptz,
  publish_targets text[] not null default '{}',   -- e.g. {'tiktok','reels','shorts'}

  -- ops
  attempt         integer not null default 0,
  max_attempts    integer not null default 5,
  last_error      text,
  error_history   jsonb not null default '[]'::jsonb,
  meta            jsonb not null default '{}'::jsonb,

  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  locked_at       timestamptz,
  locked_by       text,

  constraint jobs_idempotency_uidx unique (idempotency_key)
);

create index if not exists jobs_status_idx on jobs (status);
create index if not exists jobs_renderer_idx on jobs (renderer);
create index if not exists jobs_scheduled_idx on jobs (scheduled_for) where scheduled_for is not null;
create index if not exists jobs_created_idx on jobs (created_at desc);

-- ---------------------------------------------------------------------------
-- job_events  — append-only audit / timeline
-- ---------------------------------------------------------------------------
create table if not exists job_events (
  id          bigserial primary key,
  job_id      uuid not null references jobs(id) on delete cascade,
  from_status job_status,
  to_status   job_status not null,
  actor       text,                       -- 'worker','api','human','system'
  reason      text,
  payload     jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);
create index if not exists job_events_job_idx on job_events (job_id, created_at);

-- ---------------------------------------------------------------------------
-- webhook_deliveries  — outbound fan-out with retry
-- ---------------------------------------------------------------------------
create table if not exists webhook_deliveries (
  id            bigserial primary key,
  job_id        uuid not null references jobs(id) on delete cascade,
  target        text not null,            -- 'clickup','notion','custom'
  endpoint      text not null,
  payload       jsonb not null,
  status        text not null default 'pending',  -- pending|success|failed|dead
  attempt       integer not null default 0,
  max_attempts  integer not null default 8,
  next_attempt_at timestamptz not null default now(),
  last_error    text,
  response_code integer,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists wh_due_idx on webhook_deliveries (status, next_attempt_at)
  where status in ('pending','failed');

-- ---------------------------------------------------------------------------
-- updated_at trigger
-- ---------------------------------------------------------------------------
create or replace function set_updated_at() returns trigger as $$
begin new.updated_at = now(); return new; end; $$ language plpgsql;

drop trigger if exists trg_assets_updated on assets;
create trigger trg_assets_updated before update on assets
  for each row execute function set_updated_at();

drop trigger if exists trg_jobs_updated on jobs;
create trigger trg_jobs_updated before update on jobs
  for each row execute function set_updated_at();

drop trigger if exists trg_wh_updated on webhook_deliveries;
create trigger trg_wh_updated before update on webhook_deliveries
  for each row execute function set_updated_at();

-- ---------------------------------------------------------------------------
-- Atomic transition RPC  (the only legal way to move status)
-- ---------------------------------------------------------------------------
create or replace function transition_job(
  p_job_id        uuid,
  p_to            job_status,
  p_actor         text default 'system',
  p_reason        text default null,
  p_payload       jsonb default '{}'::jsonb,
  p_error         text default null,
  p_set_locked_by text default null
) returns jobs
language plpgsql as $$
declare
  v_from job_status;
  v_row  jobs;
begin
  select status into v_from from jobs where id = p_job_id for update;
  if not found then
    raise exception 'job % not found', p_job_id using errcode = 'P0002';
  end if;

  -- legal transitions
  if not (
    (v_from = 'draft'          and p_to in ('tts_queued','cancelled')) or
    (v_from = 'tts_queued'     and p_to in ('tts_running','tts_failed','cancelled')) or
    (v_from = 'tts_running'    and p_to in ('tts_done','tts_failed','cancelled')) or
    (v_from = 'tts_done'       and p_to in ('render_queued','cancelled')) or
    (v_from = 'tts_failed'     and p_to in ('tts_queued','dead','cancelled')) or
    (v_from = 'render_queued'  and p_to in ('render_running','render_failed','cancelled')) or
    (v_from = 'render_running' and p_to in ('render_done','render_failed','cancelled')) or
    (v_from = 'render_done'    and p_to in ('review','published','cancelled')) or
    (v_from = 'render_failed'  and p_to in ('render_queued','dead','cancelled')) or
    (v_from = 'review'         and p_to in ('approved','published','cancelled','render_queued')) or
    (v_from = 'approved'       and p_to in ('published','cancelled')) or
    (v_from = 'published'      and p_to in ('cancelled')) or
    (v_from = 'cancelled'      and p_to in ('draft')) or
    (v_from = 'dead'           and p_to in ('tts_queued','render_queued','cancelled'))
  ) then
    raise exception 'illegal transition % -> %', v_from, p_to using errcode = 'P0001';
  end if;

  update jobs
     set status      = p_to,
         last_error  = coalesce(p_error, last_error),
         error_history = case when p_error is not null
                              then error_history || jsonb_build_array(
                                jsonb_build_object('at', now(), 'from', v_from, 'to', p_to, 'error', p_error))
                              else error_history end,
         locked_by   = coalesce(p_set_locked_by, locked_by),
         locked_at   = case when p_to in ('tts_running','render_running') then now() else locked_at end,
         attempt     = case when p_to in ('tts_failed','render_failed') then attempt + 1 else attempt end
   where id = p_job_id
   returning * into v_row;

  insert into job_events (job_id, from_status, to_status, actor, reason, payload)
  values (p_job_id, v_from, p_to, p_actor, p_reason, p_payload);

  return v_row;
end;
$$;

-- ---------------------------------------------------------------------------
-- Idempotent create
-- ---------------------------------------------------------------------------
create or replace function create_job(
  p_script_text   text,
  p_voice_id      text default null,
  p_template_id   text default null,
  p_avatar_id     text default null,
  p_renderer      renderer_type default 'remotion',
  p_scheduled_for timestamptz default null,
  p_publish_targets text[] default '{}',
  p_meta          jsonb default '{}'::jsonb
) returns jobs
language plpgsql as $$
declare
  v_script_hash text;
  v_key         text;
  v_row         jobs;
begin
  v_script_hash := encode(digest(trim(both from p_script_text), 'sha256'), 'hex');
  v_key := v_script_hash || '|' || coalesce(p_voice_id,'') || '|' ||
           coalesce(p_template_id,'') || '|' || coalesce(p_avatar_id,'') || '|' || p_renderer::text;

  insert into jobs (idempotency_key, script_hash, script_text, voice_id, template_id,
                    avatar_id, renderer, scheduled_for, publish_targets, meta)
  values (v_key, v_script_hash, p_script_text, p_voice_id, p_template_id,
          p_avatar_id, p_renderer, p_scheduled_for, p_publish_targets, p_meta)
  on conflict (idempotency_key) do nothing
  returning * into v_row;

  if v_row.id is null then
    select * into v_row from jobs where idempotency_key = v_key;
  end if;
  return v_row;
end;
$$;

-- ---------------------------------------------------------------------------
-- Stuck-job sweeper (call from cron / worker)
-- ---------------------------------------------------------------------------
create or replace function reclaim_stuck_jobs(p_stale_minutes integer default 15)
returns integer
language plpgsql as $$
declare
  n integer;
begin
  with stuck as (
    select id from jobs
     where status in ('tts_running','render_running')
       and locked_at < now() - (p_stale_minutes || ' minutes')::interval
     for update skip locked
  )
  update jobs j
     set status = case when j.status = 'tts_running' then 'tts_failed'::job_status
                       else 'render_failed'::job_status end,
         last_error = 'reclaimed: worker lock stale',
         locked_at = null, locked_by = null,
         attempt = attempt + 1
    from stuck s where j.id = s.id;
  get diagnostics n = row_count;

  insert into job_events (job_id, from_status, to_status, actor, reason)
  select id,
         case when status = 'tts_failed' then 'tts_running'::job_status else 'render_running'::job_status end,
         status, 'system', 'stale lock reclaim'
    from jobs where last_error = 'reclaimed: worker lock stale'
      and updated_at > now() - interval '1 minute';

  return n;
end;
$$;
