-- PR #22 re-audit fixes. Additive only; 007 and 008 are left untouched.
--   PR22-F2: single-use render approvals (durable spend record).
--   PR21-F6 / PR22-F6: durable publish idempotency for campaign events.
--   PR22-F4 / PR22-F5: complete audio scrub across all Job Command voice JSON.
--
-- The job_command_* campaign tables are read/written only by the Job Command
-- ingress/MCP through JOB_COMMAND_CAMPAIGN_DATABASE_URL (no fallback to the MSE
-- DATABASE_URL). Point that DSN at a dedicated database/role; neither table
-- stores the bearer approval token, only its sha256.

-- ---------------------------------------------------------------- F6 ------
create table if not exists job_command_publish_log (
  idempotency_key text primary key check (idempotency_key ~ '^[0-9a-f]{64}$'),
  event_id uuid not null,
  event_type text not null,
  campaign_id uuid not null,
  status text not null default 'pending' check (status in ('pending','published','failed')),
  message_id text,
  attempts integer not null default 1,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  published_at timestamptz
);
create index if not exists job_command_publish_log_campaign_idx
  on job_command_publish_log(campaign_id, created_at desc);
drop trigger if exists trg_job_command_publish_log_updated on job_command_publish_log;
create trigger trg_job_command_publish_log_updated before update on job_command_publish_log
  for each row execute function set_updated_at();
comment on table job_command_publish_log is
  'PR21-F6: claim-before-publish dedupe for Job Command Pub/Sub events; duplicates return the prior message_id.';

-- ---------------------------------------------------------------- F2 ------
create table if not exists job_command_spent_approvals (
  token_sha256 text primary key check (token_sha256 ~ '^[0-9a-f]{64}$'),
  approval_id text not null,
  campaign_id uuid not null,
  idempotency_key text not null references job_command_publish_log(idempotency_key) on delete restrict,
  event_id uuid not null,
  approved_by text not null default '',
  expires_at timestamptz,
  spent_at timestamptz not null default now(),
  unique (campaign_id, approval_id)
);
comment on table job_command_spent_approvals is
  'PR22-F2: each render approval (by token hash and by campaign+approval_id) authorizes exactly one render event.';

-- -------------------------------------------------------- F4 / F5 scrub ---
-- Mirrors app/services/voice_sessions.py AUDIO_FIELD_NAMES + blob heuristic.
create or replace function job_command_redact_audio_jsonb(j jsonb) returns jsonb
language plpgsql immutable as $fn$
declare
  k text;
  v jsonb;
  s text;
  result jsonb;
  audio_keys constant text[] := array[
    'full_audio','audio','audio_base64','audio_b64','audio_data',
    'raw_audio','audio_chunk','audio_content','recording'];
begin
  if j is null then
    return null;
  end if;
  if jsonb_typeof(j) = 'object' then
    result := '{}'::jsonb;
    for k, v in select key, value from jsonb_each(j) loop
      if lower(k) = any(audio_keys) and jsonb_typeof(v) <> 'null' then
        if jsonb_typeof(v) = 'object' and v->>'redacted' = 'true' then
          result := result || jsonb_build_object(k, v);  -- already a redaction summary
        elsif jsonb_typeof(v) = 'string' then
          s := v #>> '{}';
          result := result || jsonb_build_object(k, jsonb_build_object(
            'redacted', true, 'encoded_chars', length(s),
            'sha256', encode(sha256(convert_to(s, 'UTF8')), 'hex')));
        else
          result := result || jsonb_build_object(k, jsonb_build_object('redacted', true));
        end if;
      else
        result := result || jsonb_build_object(k, job_command_redact_audio_jsonb(v));
      end if;
    end loop;
    return result;
  elsif jsonb_typeof(j) = 'array' then
    select coalesce(jsonb_agg(job_command_redact_audio_jsonb(e) order by ord), '[]'::jsonb)
      into result from jsonb_array_elements(j) with ordinality as t(e, ord);
    return result;
  elsif jsonb_typeof(j) = 'string' then
    s := j #>> '{}';
    if length(s) >= 1024 and s ~ '^[A-Za-z0-9+/_-]+={0,2}$' then
      return jsonb_build_object('redacted', true, 'encoded_chars', length(s),
                                'sha256', encode(sha256(convert_to(s, 'UTF8')), 'hex'));
    end if;
    return j;
  end if;
  return j;
end
$fn$;

comment on function job_command_redact_audio_jsonb(jsonb) is
  'PR22-F5: recursively redacts audio-named keys and long base64 blobs (same rules as voice_sessions.redact_binary_fields).';

-- Matched events: redact every audio key / blob anywhere in the payload.
update voice_session_events
   set payload = job_command_redact_audio_jsonb(payload)
 where session_id is not null
   and payload is distinct from job_command_redact_audio_jsonb(payload);

-- Unmatched events: rebuild as a true metadata-only envelope (same fields as
-- voice_sessions.storage_payload(matched_session=False)).
update voice_session_events e
   set payload = jsonb_build_object(
         'type', e.payload->'type',
         'event_timestamp', e.payload->'event_timestamp',
         'data', coalesce((
            select jsonb_object_agg(d.key, d.value)
              from jsonb_each(case when jsonb_typeof(e.payload->'data') = 'object'
                                   then e.payload->'data' else '{}'::jsonb end) d
             where d.key in ('agent_id','conversation_id','status','start_time_unix_secs','call_duration_secs')
               and jsonb_typeof(d.value) not in ('object','array')
         ), '{}'::jsonb),
         'minimal_envelope', true,
         'audio_redacted', coalesce((e.payload->>'audio_redacted')::boolean, false)
            or exists (
              select 1 from jsonb_object_keys(case when jsonb_typeof(e.payload->'data') = 'object'
                                                   then e.payload->'data' else '{}'::jsonb end) k
               where lower(k) in ('full_audio','audio','audio_base64','audio_b64','audio_data',
                                  'raw_audio','audio_chunk','audio_content','recording')))
 where e.session_id is null;

-- F4: transcript-turn meta must never carry audio. Audio-named top-level keys
-- are dropped (runtime _turn_meta behaviour); nested blobs are redacted.
update voice_transcript_turns
   set meta = job_command_redact_audio_jsonb(
                meta - array['full_audio','audio','audio_base64','audio_b64','audio_data',
                             'raw_audio','audio_chunk','audio_content','recording'])
 where meta is distinct from job_command_redact_audio_jsonb(
                meta - array['full_audio','audio','audio_base64','audio_b64','audio_data',
                             'raw_audio','audio_chunk','audio_content','recording']);

update voice_transcript_turns
   set text = '[redacted binary content]'
 where length(text) >= 1024 and text ~ '^[A-Za-z0-9+/_-]+={0,2}$';

-- Other JSON columns on Job Command voice tables (defensive; none should hold audio).
update voice_sessions set metadata = job_command_redact_audio_jsonb(metadata)
 where metadata is distinct from job_command_redact_audio_jsonb(metadata);
update voice_work_queue set payload = job_command_redact_audio_jsonb(payload)
 where payload is distinct from job_command_redact_audio_jsonb(payload);
update voice_transcription_runs set config = job_command_redact_audio_jsonb(config)
 where config is distinct from job_command_redact_audio_jsonb(config);
