-- PR #21 audit fixes (Job Command voice).
--   PR21-F1: never retain raw post-call audio in voice_session_events.payload.
--   PR21-F4: durable, instance-safe accounting for ElevenLabs signed-URL mints.
-- Additive only; 20260317000007 is left untouched.

-- F4: every mint attempt is reserved here BEFORE the provider is called, so a
-- failed or retried provider call still consumes budget (fail closed). Only
-- fingerprints of the caller key and client IP are stored, never raw values.
create table if not exists voice_mint_attempts (
  id bigserial primary key,
  day_utc date not null default ((now() at time zone 'utc')::date),
  api_key_fingerprint text not null,
  client_ip_fingerprint text not null,
  visitor_ref text not null,
  outcome text not null default 'reserved'
    check (outcome in ('reserved','minted','failed')),
  session_id uuid references voice_sessions(id) on delete set null,
  created_at timestamptz not null default now()
);
create index if not exists voice_mint_attempts_day_idx
  on voice_mint_attempts(day_utc);
create index if not exists voice_mint_attempts_key_idx
  on voice_mint_attempts(api_key_fingerprint, created_at desc);
create index if not exists voice_mint_attempts_ip_idx
  on voice_mint_attempts(client_ip_fingerprint, created_at desc);
create index if not exists voice_mint_attempts_visitor_idx
  on voice_mint_attempts(visitor_ref, created_at desc);

comment on table voice_mint_attempts is
  'PR21-F4: reserved ElevenLabs signed-URL mint attempts for per-key/IP/visitor rate limits and a global UTC-day cap.';

-- F1: scrub any raw audio persisted by the pre-fix code path. Payloads keep a
-- redaction marker instead of the base64 body.
update voice_session_events
   set payload = (payload #- '{data,full_audio}')
                 || jsonb_build_object('audio_redacted', true)
 where jsonb_typeof(payload->'data') = 'object'
   and (payload->'data') ? 'full_audio';

-- F1: unmatched callbacks are retained as metadata-only envelopes.
update voice_session_events
   set payload = (payload #- '{data,transcript}')
                 || jsonb_build_object('minimal_envelope', true)
 where session_id is null
   and jsonb_typeof(payload->'data') = 'object'
   and (payload->'data') ? 'transcript';
