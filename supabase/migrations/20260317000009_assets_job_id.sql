-- Real canonical fix for a pre-existing gap: app/services/pipeline.py and
-- app/services/jobs.py have always recorded assets as job-scoped
-- (INSERT INTO assets(job_id, kind, ...) / SELECT * FROM assets WHERE job_id=$1
-- / ON CONFLICT (job_id, kind)), but the original canonical assets table
-- (20260317000001) never had a job_id column — only the test-only
-- tests/_sot_app_compat.sql overlay added one, so no test ever ran this
-- code path against a real canonical database until now (see Phase 26 /
-- test_canonical_pipeline_lifecycle.py). This adds the same real column and
-- constraint to the actual deploy schema instead of leaving it compat-only.
--
-- assets.job_id stays nullable: assets can still exist independently
-- (content-addressed, deduped via content_hash) — this only makes the
-- already-shipped job-scoped recording pattern legal on a real deploy.

alter table assets
  add column if not exists job_id uuid references jobs(id) on delete cascade;

create index if not exists assets_job_id_idx on assets (job_id);

create unique index if not exists assets_job_id_kind_uidx
  on assets (job_id, kind) where job_id is not null;

-- Same real-schema gap for asset_kind: pipeline.py has always inserted
-- kind='final' (both the legacy HeyGen render() and the new render_vertex())
-- and, on the Vertex path, kind='video_clip' — neither value exists in the
-- original enum (20260317000001) or anywhere in the test compat overlay.
-- ALTER TYPE ... ADD VALUE cannot run inside the same transaction block as
-- other DDL that might use it, so these are standalone statements.
alter type asset_kind add value if not exists 'final';
alter type asset_kind add value if not exists 'video_clip';
