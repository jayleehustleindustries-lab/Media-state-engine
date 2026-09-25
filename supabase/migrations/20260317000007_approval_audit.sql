-- Media State Engine — migration 007: real approval audit columns
-- Target: Supabase (Postgres 15+)
-- Depends on: 001 (jobs, job_status), 002 (api_keys)
--
-- app/services/jobs.py::approve_job() records who moved a job into
-- 'approved' and when. These are meaningful, permanent audit fields
-- (compliance / accountability for the human approval gate), not
-- test-only scaffolding — a prior version of this app only had them in
-- tests/_sot_app_compat.sql, which meant approve_job() could never
-- actually succeed against a real deploy of supabase/migrations/*.sql.
--
-- approved_by is a human-readable label (an api_keys.name when the
-- caller's credential resolves to a real row there, else a
-- non-spoofable fallback derived from the presented credential).
-- approved_by_key_id is the real, non-spoofable FK: it is only ever set
-- server-side from a verified api_keys row (via verify_api_key()),
-- never from client-supplied text — see app/services/identity.py.

alter table jobs
  add column if not exists approved_at timestamptz,
  add column if not exists approved_by text,
  add column if not exists approved_by_key_id uuid references api_keys(id) on delete set null;

create index if not exists jobs_approved_at_idx on jobs (approved_at) where approved_at is not null;
