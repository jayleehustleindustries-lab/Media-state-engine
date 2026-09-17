import os
from pathlib import Path

import pytest

# Ensure auth is configured for app-level tests unless a test overrides.
os.environ.setdefault("MEDIA_ENGINE_API_KEY", "test-api-key-phase1")
os.environ.setdefault("API_KEY", "test-api-key-phase1")


def pytest_configure(config):
    config.addinivalue_line("markers", "postgres: requires real DATABASE_URL / Postgres")


@pytest.fixture(scope="session")
def database_url():
    return os.environ.get("DATABASE_URL", "").strip()


@pytest.fixture(scope="session")
def require_database_url(database_url):
    if not database_url:
        pytest.skip("DATABASE_URL not set — skipping real Postgres tests")
    return database_url


async def apply_schema(conn):
    """Reset public schema, apply deploy SoT, then test-only app compat overlay.

    Deploy SoT remains supabase/migrations/*.sql. tests/_sot_app_compat.sql is
    pytest-only so Phase 1–5 app code (events/webhook_outbox/legacy statuses)
    can run until those paths are fully ported to SoT.
    """
    await conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    await conn.execute("CREATE SCHEMA public")
    await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    root = Path(__file__).resolve().parents[1]
    mig_dir = root / "supabase" / "migrations"
    for path in sorted(mig_dir.glob("*.sql")):
        await conn.execute(path.read_text())
    compat = root / "tests" / "_sot_app_compat.sql"
    if compat.exists():
        await conn.execute(compat.read_text())


async def truncate_app_tables(conn):
    """Truncate all public tables (SoT names differ from legacy webhook_outbox/events)."""
    await conn.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN (
            SELECT tablename FROM pg_tables
             WHERE schemaname = 'public'
          ) LOOP
            EXECUTE format('TRUNCATE TABLE %I RESTART IDENTITY CASCADE', r.tablename);
          END LOOP;
        END $$;
        """
    )



@pytest.fixture(autouse=True)
def soft_image_gate_for_legacy_pipeline(request, monkeypatch):
    """Opt-in stub for legacy HeyGen unit paths (#19).

    Only tests marked ``@pytest.mark.soft_image_gate`` skip the real likeness
    gate. Default / unmarked tests (incl. ``test_image_gate``) stay fail-closed.
    """
    if request.node.get_closest_marker("soft_image_gate") is None:
        return

    async def _pass(conn, job_id):
        return {
            "id": "00000000-0000-0000-0000-000000000001",
            "verdict": "pass",
            "overall": 9,
            "identity_likeness": 9,
            "ref_content_hash": "test-ref-hash",
        }

    monkeypatch.setattr("app.services.pipeline.assert_pass_for_heygen", _pass)
