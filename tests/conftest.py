import os
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
