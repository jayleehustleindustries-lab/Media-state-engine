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
    """Reset public schema and apply deploy SoT: supabase/migrations/*.sql."""
    await conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    await conn.execute("CREATE SCHEMA public")
    await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    root = Path(__file__).resolve().parents[1]
    mig_dir = root / "supabase" / "migrations"
    for path in sorted(mig_dir.glob("*.sql")):
        await conn.execute(path.read_text())
