# Media State Engine — Supabase migrations

## Files

1. `001_media_state_engine_core.sql` — enums, `assets`, `jobs`, `job_events`, `webhook_deliveries`, RPCs `transition_job` / `create_job` / `reclaim_stuck_jobs`
2. `002_media_state_engine_auth_and_quality.sql` — `api_keys`, `content_quality_scores`, `retry_policies` + `job_dead_letters`, indexes/constraints, patched `transition_job`, `claim_job`, fixed reclaim, RLS on

Run **001 then 002**. Do not skip.

## How to run in Supabase

### Option A — SQL Editor (fastest)

1. Open Supabase Dashboard → **SQL Editor**
2. Paste contents of `001_media_state_engine_core.sql` → Run
3. Paste contents of `002_media_state_engine_auth_and_quality.sql` → Run
4. Confirm: `select typname from pg_type where typname like 'job_%' or typname in ('renderer_type','asset_kind','asset_provider','quality_verdict');`

### Option B — Supabase CLI

```bash
# from a linked project
supabase db execute -f 001_media_state_engine_core.sql
supabase db execute -f 002_media_state_engine_auth_and_quality.sql
```

Or place them under `supabase/migrations/` with timestamps and `supabase db push`.

### API key bootstrap (after 002)

Generate a long random key in the app, store **only** the hash:

```sql
-- Example: raw key shown once to ops; never store raw in DB
-- raw = 'mse_live_...' (keep offline)
insert into api_keys (name, key_prefix, key_hash, scopes)
values (
  'fastapi-prod',
  left('mse_live_REPLACE', 8),
  encode(digest('mse_live_REPLACE_WITH_REAL_KEY', 'sha256'), 'hex'),
  array['jobs:read','jobs:write','admin']
);
```

FastAPI should send `Authorization: Bearer <raw>` and call `verify_api_key` (or compare hash in app). Prefer app-side hash compare if you want to avoid `security definer` RPC from the client.

## How the worker should call `transition_job`

**Rule:** the worker never `UPDATE jobs SET status=...` directly. Status moves only via RPC.

### Claim work (paid steps)

```sql
-- Claim up to 1 TTS job for this worker
select * from claim_job('tts_queued', 'tts_running', 'worker-1', 1);

-- Claim render
select * from claim_job('render_queued', 'render_running', 'worker-1', 1);
```

`claim_job` uses `FOR UPDATE SKIP LOCKED` so two workers cannot double-call ElevenLabs/HeyGen.

### Advance after provider success / failure

```sql
-- TTS succeeded → enqueue render
select * from transition_job(
  p_job_id := '...'::uuid,
  p_to := 'tts_done',
  p_actor := 'worker-1',
  p_reason := 'elevenlabs ok',
  p_payload := '{"provider":"elevenlabs"}'::jsonb
);

select * from transition_job('...'::uuid, 'render_queued', 'worker-1');

-- Render succeeded → human review (NOT published)
select * from transition_job('...'::uuid, 'render_done', 'worker-1');
select * from transition_job('...'::uuid, 'review', 'worker-1');
```

### Human approval then publish

```sql
select * from transition_job('...'::uuid, 'approved', 'human', 'cli approve');
-- only then:
select * from transition_job('...'::uuid, 'published', 'distributor', 'tiktok upload ok');
```

### Quality gate (002)

Before `draft → tts_queued|render_queued`, insert a passing score:

```sql
insert into content_quality_scores
  (job_id, script_hash, hook_strength, clarity, cta, pacing, verdict, scorer)
values
  ('...'::uuid, '<jobs.script_hash>', 80, 75, 70, 78, 'pass', 'bot');
```

`transition_job(..., p_require_quality_pass => true)` (default) blocks enqueue without `pass`.

### Stuck reclaim (cron every 5–15 min)

```sql
select reclaim_stuck_jobs(15);
```

### HeyGen-only path

002 allows `draft → render_queued` (skip TTS) when HeyGen does voice+avatar in one call. Still requires quality `pass` unless you pass `p_require_quality_pass => false` (ops only).

## Stress-test note

`001` alone allowed `render_done → published` and `review → published`, skipping approval. **002 removes those edges.** Always deploy 002 before any production worker that publishes.
