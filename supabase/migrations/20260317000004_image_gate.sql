-- NOTE: ADD VALUE IF NOT EXISTS for image_* statuses must run OUTSIDE a transaction
-- before this file when using older Postgres; on PG 15+ IF NOT EXISTS is fine.
ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'image_queued' AFTER 'draft';
ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'image_running' AFTER 'image_queued';
ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'image_ready' AFTER 'image_running';
ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'image_failed' AFTER 'image_ready';
ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'needs_human_review' AFTER 'image_failed';

create table if not exists image_scores (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references jobs(id) on delete cascade,
  attempt_n integer not null check (attempt_n >= 1),
  face_consistency numeric(4,2) not null check (face_consistency between 0 and 10),
  lighting numeric(4,2) not null check (lighting between 0 and 10),
  composition numeric(4,2) not null check (composition between 0 and 10),
  text_legibility numeric(4,2) not null check (text_legibility between 0 and 10),
  brand_fit numeric(4,2) not null check (brand_fit between 0 and 10),
  no_artifacts numeric(4,2) not null check (no_artifacts between 0 and 10),
  identity_likeness numeric(4,2) not null check (identity_likeness between 0 and 10),
  overall numeric(4,2) not null check (overall between 0 and 10),
  identity_drift boolean not null default false,
  verdict text not null check (verdict in ('pass','fail','error')),
  scorer text not null default 'gemini',
  failure_reasons text[] not null default '{}',
  prompt_hash text,
  candidate_asset_id uuid references assets(id) on delete set null,
  candidate_url text,
  pack_id text not null,
  manifest_id text,
  hero_ref_file_id text,
  ref_content_hash text not null,
  likeness_approval_status text not null default 'approved',
  meta jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (job_id, attempt_n)
);

create index if not exists image_scores_job_idx on image_scores (job_id, created_at desc);
create index if not exists image_scores_pass_idx
  on image_scores (job_id, ref_content_hash) where verdict = 'pass';

alter table jobs
  add column if not exists image_attempt integer not null default 0,
  add column if not exists image_score_id uuid references image_scores(id) on delete set null,
  add column if not exists image_ref_content_hash text,
  add column if not exists scene_image_asset_id uuid references assets(id) on delete set null;

-- Extended transition_job: image gate + sacred approval publish path

CREATE OR REPLACE FUNCTION public.transition_job(p_job_id uuid, p_to job_status, p_actor text DEFAULT 'system'::text, p_reason text DEFAULT NULL::text, p_payload jsonb DEFAULT '{}'::jsonb, p_error text DEFAULT NULL::text, p_set_locked_by text DEFAULT NULL::text, p_require_quality_pass boolean DEFAULT true, p_require_image_pass boolean DEFAULT true)
 RETURNS jobs
 LANGUAGE plpgsql
AS $function$
declare
  v_from job_status;
  v_row  jobs;
  v_verdict quality_verdict;
  v_img image_scores;
begin
  select status into v_from from jobs where id = p_job_id for update;
  if not found then
    raise exception 'job % not found', p_job_id using errcode = 'P0002';
  end if;

  if not (
    (v_from = 'draft' and p_to in ('image_queued','tts_queued','render_queued','cancelled')) or
    (v_from = 'image_queued' and p_to in ('image_running','image_failed','cancelled','needs_human_review')) or
    (v_from = 'image_running' and p_to in ('image_ready','image_failed','needs_human_review','cancelled')) or
    (v_from = 'image_ready' and p_to in ('tts_queued','render_queued','cancelled')) or
    (v_from = 'image_failed' and p_to in ('image_queued','needs_human_review','dead','cancelled')) or
    (v_from = 'needs_human_review' and p_to in ('image_queued','draft','cancelled','dead')) or
    (v_from = 'tts_queued' and p_to in ('tts_running','tts_failed','cancelled')) or
    (v_from = 'tts_running' and p_to in ('tts_done','tts_failed','cancelled')) or
    (v_from = 'tts_done' and p_to in ('render_queued','cancelled')) or
    (v_from = 'tts_failed' and p_to in ('tts_queued','dead','cancelled')) or
    (v_from = 'render_queued' and p_to in ('render_running','render_failed','cancelled')) or
    (v_from = 'render_running' and p_to in ('render_done','render_failed','cancelled')) or
    (v_from = 'render_done' and p_to in ('review','cancelled')) or
    (v_from = 'render_failed' and p_to in ('render_queued','dead','cancelled')) or
    (v_from = 'review' and p_to in ('approved','cancelled','render_queued')) or
    (v_from = 'approved' and p_to in ('published','cancelled')) or
    (v_from = 'published' and p_to in ('cancelled')) or
    (v_from = 'cancelled' and p_to in ('draft')) or
    (v_from = 'dead' and p_to in ('tts_queued','render_queued','image_queued','cancelled'))
  ) then
    raise exception 'illegal transition % -> %', v_from, p_to using errcode = 'P0001';
  end if;

  if p_require_quality_pass and v_from = 'draft' and p_to in ('image_queued','tts_queued','render_queued') then
    v_verdict := latest_quality_verdict(p_job_id);
    if v_verdict is distinct from 'pass' then
      raise exception 'quality gate blocked: latest verdict=% (need pass)', coalesce(v_verdict::text, 'null')
        using errcode = 'P0003';
    end if;
  end if;

  if p_require_image_pass and v_from = 'draft' and p_to in ('tts_queued','render_queued') then
    if coalesce((p_payload->>'break_glass_image_gate')::boolean, false) is not true then
      raise exception 'image gate required: use draft -> image_queued first' using errcode = 'P0004';
    end if;
  end if;

  if p_require_image_pass and v_from = 'image_ready' and p_to in ('tts_queued','render_queued') then
    select * into v_img from image_scores
     where job_id = p_job_id and verdict = 'pass'
     order by created_at desc limit 1;
    if not found then
      raise exception 'image gate blocked: no passing image_scores row' using errcode = 'P0004';
    end if;
    if v_img.overall < 8 or v_img.identity_likeness < 8 or v_img.identity_drift then
      raise exception 'image gate blocked: overall/identity veto' using errcode = 'P0004';
    end if;
    if v_img.ref_content_hash is distinct from (select image_ref_content_hash from jobs where id = p_job_id) then
      raise exception 'image gate blocked: ref_content_hash mismatch' using errcode = 'P0004';
    end if;
  end if;

  update jobs
     set status = p_to,
         last_error = case when p_error is not null then p_error
                           when p_to in ('tts_done','render_done','image_ready','approved','published') then null
                           else last_error end,
         error_history = case when p_error is not null
                              then error_history || jsonb_build_array(
                                jsonb_build_object('at', now(), 'from', v_from, 'to', p_to, 'error', p_error))
                              else error_history end,
         locked_by = case
                       when p_to in ('tts_running','render_running','image_running') then coalesce(p_set_locked_by, locked_by)
                       when p_to in ('tts_done','tts_failed','render_done','render_failed','image_ready','image_failed','cancelled','dead','needs_human_review') then null
                       else locked_by end,
         locked_at = case
                       when p_to in ('tts_running','render_running','image_running') then now()
                       when p_to in ('tts_done','tts_failed','render_done','render_failed','image_ready','image_failed','cancelled','dead','needs_human_review') then null
                       else locked_at end,
         attempt = case when p_to in ('tts_failed','render_failed') then attempt + 1 else attempt end,
         published_at = case when p_to = 'published' then coalesce(published_at, now()) else published_at end
   where id = p_job_id
   returning * into v_row;

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
$function$;

CREATE OR REPLACE FUNCTION public.claim_job(p_from job_status, p_to job_status, p_worker text, p_limit integer DEFAULT 1)
 RETURNS SETOF jobs
 LANGUAGE plpgsql
AS $function$
begin
  if not (
    (p_from = 'tts_queued' and p_to = 'tts_running') or
    (p_from = 'render_queued' and p_to = 'render_running') or
    (p_from = 'image_queued' and p_to = 'image_running')
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
         locked_at = now(),
         image_attempt = case when p_to = 'image_running' then j.image_attempt + 1 else j.image_attempt end
    from picked p
   where j.id = p.id
  returning j.*;

  insert into job_events (job_id, from_status, to_status, actor, reason)
  select id, p_from, p_to, p_worker, 'claim'
    from jobs
   where locked_by = p_worker and status = p_to and locked_at > now() - interval '2 seconds';
end;
$function$;

CREATE OR REPLACE FUNCTION public.assert_image_pass_for_heygen(p_job_id uuid)
 RETURNS image_scores
 LANGUAGE plpgsql
AS $function$
declare
  v_job jobs;
  v_score image_scores;
begin
  select * into v_job from jobs where id = p_job_id for update;
  if not found then
    raise exception 'job % not found', p_job_id using errcode = 'P0002';
  end if;
  if v_job.status not in ('image_ready','tts_done','tts_queued','render_queued','render_running','tts_running') then
    raise exception 'job % status % not eligible for heygen', p_job_id, v_job.status
      using errcode = 'P0004';
  end if;
  select * into v_score from image_scores
   where job_id = p_job_id and verdict = 'pass'
   order by created_at desc limit 1;
  if not found then
    raise exception 'no passing image score for job %', p_job_id using errcode = 'P0004';
  end if;
  if v_score.overall < 8 or v_score.identity_likeness < 8 or v_score.identity_drift then
    raise exception 'image score veto overall=% identity=% drift=%',
      v_score.overall, v_score.identity_likeness, v_score.identity_drift using errcode = 'P0004';
  end if;
  if v_job.image_ref_content_hash is null
     or v_score.ref_content_hash is distinct from v_job.image_ref_content_hash then
    raise exception 'ref_content_hash mismatch — re-score required' using errcode = 'P0004';
  end if;
  return v_score;
end;
$function$;


INSERT INTO retry_policies (id, max_attempts, base_delay_sec, max_delay_sec, dead_after)
VALUES ('image_gate', 3, 10, 300, 3)
ON CONFLICT (id) DO NOTHING;
