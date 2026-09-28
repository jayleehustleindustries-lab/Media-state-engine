-- Job Command live voice sessions: provider-safe control plane + transcript/audit data.
-- Secrets and bearer signed URLs never enter these tables.

create table if not exists voice_agent_profiles (
  id uuid primary key default gen_random_uuid(),
  code text not null unique,
  provider text not null default 'elevenlabs',
  provider_agent_id text not null,
  owner_voice_ref text not null,
  owner_avatar_ref text not null,
  default_language text not null,
  supported_languages text[] not null,
  presentation_policy jsonb not null default '{}'::jsonb,
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (cardinality(supported_languages) > 0)
);

create table if not exists voice_sessions (
  id uuid primary key default gen_random_uuid(),
  profile_id uuid not null references voice_agent_profiles(id) on delete restrict,
  visitor_ref text not null,
  requested_language text,
  resolved_language text not null,
  surface text not null default 'web' check (surface in ('web','phone')),
  state text not null default 'issued'
    check (state in ('issued','connected','completed','archived','failed','expired')),
  provider_conversation_id text unique,
  provider_agent_id text not null,
  token_issued_at timestamptz not null default now(),
  token_expires_at timestamptz not null,
  connected_at timestamptz,
  completed_at timestamptz,
  archive_consent boolean not null default false,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists voice_sessions_visitor_idx on voice_sessions(visitor_ref, created_at desc);
create index if not exists voice_sessions_state_idx on voice_sessions(state, created_at desc);

create table if not exists voice_session_events (
  id bigserial primary key,
  session_id uuid references voice_sessions(id) on delete set null,
  provider_event_key text not null unique,
  provider_event_type text not null,
  provider_conversation_id text,
  payload jsonb not null default '{}'::jsonb,
  received_at timestamptz not null default now(),
  processed_at timestamptz
);
create index if not exists voice_session_events_session_idx on voice_session_events(session_id, received_at);

create table if not exists voice_transcript_turns (
  id bigserial primary key,
  session_id uuid not null references voice_sessions(id) on delete cascade,
  source text not null check (source in ('elevenlabs','gemini')),
  ordinal integer not null,
  speaker_role text not null check (speaker_role in ('user','agent','speaker','unknown')),
  speaker_label text,
  language text,
  text text not null,
  start_ms integer,
  end_ms integer,
  is_final boolean not null default true,
  meta jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (session_id, source, ordinal)
);
create index if not exists voice_transcript_turns_session_idx on voice_transcript_turns(session_id, ordinal);

create table if not exists voice_transcription_runs (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references voice_sessions(id) on delete cascade,
  provider text not null default 'gemini',
  model text not null,
  mode text not null check (mode in ('diarized','smart')),
  status text not null default 'queued' check (status in ('queued','running','completed','failed')),
  source_storage_path text,
  transcript_text text,
  config jsonb not null default '{}'::jsonb,
  last_error text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz
);
create index if not exists voice_transcription_runs_session_idx on voice_transcription_runs(session_id, created_at desc);

create table if not exists voice_work_queue (
  id bigserial primary key,
  session_id uuid not null references voice_sessions(id) on delete cascade,
  run_id uuid references voice_transcription_runs(id) on delete cascade,
  step text not null check (step in ('gemini_archive')),
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'pending' check (status in ('pending','running','done','failed','dead')),
  attempts integer not null default 0,
  max_attempts integer not null default 3,
  last_error text,
  next_attempt_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);
create unique index if not exists voice_work_queue_active_session_step_uidx
  on voice_work_queue(session_id, step) where status in ('pending','running');
create index if not exists voice_work_queue_pending_idx
  on voice_work_queue(status, next_attempt_at) where status in ('pending','running');

-- The core migration owns set_updated_at().
drop trigger if exists trg_voice_agent_profiles_updated on voice_agent_profiles;
create trigger trg_voice_agent_profiles_updated before update on voice_agent_profiles
  for each row execute function set_updated_at();
drop trigger if exists trg_voice_sessions_updated on voice_sessions;
create trigger trg_voice_sessions_updated before update on voice_sessions
  for each row execute function set_updated_at();
drop trigger if exists trg_voice_work_queue_updated on voice_work_queue;
create trigger trg_voice_work_queue_updated before update on voice_work_queue
  for each row execute function set_updated_at();
